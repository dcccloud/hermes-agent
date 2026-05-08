"""
通用RabbitMQ客户端 - 双Channel架构

可复用组件，零依赖（除了pika），通过构造器注入配置和日志
"""

import time
import threading
import queue as queue_module
import platform
from typing import Callable, Optional, Dict, Any

import pika
from pika.exceptions import ConnectionClosed, AMQPConnectionError, ChannelWrongStateError, AMQPChannelError


# 常量
MIN_MESSAGE_SIZE = 2  # 最小消息大小（字节），如 "{}"
DEFAULT_HEARTBEAT = 60
DEFAULT_CONNECTION_ATTEMPTS = 3
DEFAULT_RETRY_DELAY = 1.0
DEFAULT_SOCKET_TIMEOUT = 30
DEFAULT_BLOCKED_TIMEOUT = 300


class RabbitMQClient:
    """
    通用RabbitMQ客户端 - 双Channel架构
    
    特性：
    - 双Channel：consumer_channel + publisher_channel
    - 线程安全：publish自动加锁
    - 自动重连：分级恢复策略
    - 长任务支持：20分钟+阻塞任务
    """
    
    def __init__(self, config: Dict[str, Any], logger=None):
        """
        初始化客户端
        
        Args:
            config: 配置字典，必须包含：
                {
                    "host": "localhost",
                    "port": 5672,
                    "username": "guest",
                    "password": "guest",
                    "virtual_host": "/",
                    "heartbeat": 60,
                    "connection_attempts": 5,
                    "retry_delay": 1.0,
                    "socket_timeout": 30,
                    "blocked_connection_timeout": 300
                }
            logger: 日志对象（任何有info/warning/error方法的对象）
                   支持loguru、logging或自定义logger
                   默认使用简单的print输出
        """
        self.connection = None
        self.consumer_channel = None
        self.publisher_channel = None
        
        # 配置
        self.config = config
        self.reconnect_delay = config.get("retry_delay", 1.0)
        
        # 日志（接受任何有info/warning/error方法的对象）
        self.logger = logger or self._create_simple_logger()
        
        # 发布锁
        self._publisher_lock = threading.Lock()
        
        # 消费者状态
        self._consumer_queue: Optional[str] = None
        self._consumer_callback: Optional[Callable] = None
        self._consumer_active: bool = False
        self._consumer_exchange: Optional[str] = None
        self._consumer_routing_key: Optional[str] = None
        self._consumer_tag: Optional[str] = None  # 保存consumer_tag，用于取消
        
        # 订阅元数据（用于重连后恢复）
        self._consumer_meta: Optional[Dict[str, Any]] = None
        
        # 连接参数
        self._credentials = pika.PlainCredentials(
            config["username"],
            config["password"]
        )
        
        # TCP Keep-Alive（Windows 兼容）
        tcp_options = self._get_tcp_options()
        
        self._connection_parameters = pika.ConnectionParameters(
            host=config["host"],
            port=config["port"],
            virtual_host=config["virtual_host"],
            credentials=self._credentials,
            heartbeat=config.get("heartbeat", DEFAULT_HEARTBEAT),
            connection_attempts=config.get("connection_attempts", DEFAULT_CONNECTION_ATTEMPTS),
            retry_delay=config.get("retry_delay", DEFAULT_RETRY_DELAY),
            socket_timeout=config.get("socket_timeout", DEFAULT_SOCKET_TIMEOUT),
            blocked_connection_timeout=config.get("blocked_connection_timeout", DEFAULT_BLOCKED_TIMEOUT),
            tcp_options=tcp_options
        )
    
    @staticmethod
    def _create_simple_logger():
        """创建简单logger（如果未提供）"""
        class SimpleLogger:
            def info(self, msg): print(f"[INFO] {msg}")
            def warning(self, msg): print(f"[WARNING] {msg}")
            def error(self, msg): print(f"[ERROR] {msg}")
            def debug(self, msg): pass  # 默认不输出 debug
        return SimpleLogger()
    
    @staticmethod
    def _get_tcp_options() -> Optional[Dict]:
        """获取 TCP Keep-Alive 配置（跨平台兼容）"""
        if platform.system() == 'Windows':
            # Windows 不支持这些 socket 选项
            return None
        return {
            'TCP_KEEPIDLE': 60,
            'TCP_KEEPINTVL': 10,
            'TCP_KEEPCNT': 6
        }
    
    def connect(self) -> bool:
        """连接RabbitMQ"""
        try:
            # 清理旧连接（防止连接泄漏）
            self._cleanup_old_connection()
            
            self.connection = pika.BlockingConnection(self._connection_parameters)
            self.consumer_channel = self.connection.channel()
            self.publisher_channel = self.connection.channel()
            self.publisher_channel.confirm_delivery()
            
            self.logger.info(f"连接成功 consumer={self.consumer_channel.channel_number} "
                           f"publisher={self.publisher_channel.channel_number}")
            
            return True
        except Exception as e:
            self.logger.error(f"连接失败 error={e}")
            return False
    
    def is_connected(self) -> bool:
        """检查连接状态（温和检查，避免误判）"""
        try:
            if not self.connection:
                return False
            if self.connection.is_closed:
                return False
            if not self.connection.is_open:
                return False
            if not self.consumer_channel or self.consumer_channel.is_closed:
                return False
            if not self.publisher_channel or self.publisher_channel.is_closed:
                return False
            if not (self.consumer_channel.is_open and self.publisher_channel.is_open):
                return False
            # 温和测试连接（不抛出异常，只检查状态）
            try:
                self.connection.process_data_events(time_limit=0)
            except Exception:
                # process_data_events 异常不一定表示连接断开，可能是临时问题
                # 只检查基本状态，不依赖 process_data_events 的结果
                pass
            return True
        except Exception:
            return False
    
    def publish(self, exchange: str, routing_key: str, message: str, 
                properties: Optional[pika.BasicProperties] = None) -> bool:
        """
        发布消息（线程安全，增强重试机制）
        
        Args:
            exchange: Exchange名称
            routing_key: Routing Key
            message: 消息内容
            properties: 消息属性，默认持久化
        
        Returns:
            bool: True=发送成功，False=失败
        """
        with self._publisher_lock:
            max_retries = self.config.get("connection_attempts", DEFAULT_CONNECTION_ATTEMPTS)
            retry_delay = self.config.get("retry_delay", DEFAULT_RETRY_DELAY)
            
            for attempt in range(1, max_retries + 1):
                try:
                    # 发送前检查连接状态（实际测试）
                    if not self.is_connected():
                        self.logger.warning(f"连接不可用，尝试恢复（第 {attempt} 次尝试）")
                        if not self._recover_publisher():
                            if attempt < max_retries:
                                delay = retry_delay * attempt
                                self.logger.info(f"等待 {delay} 秒后重试...")
                                time.sleep(delay)
                                continue
                            else:
                                self.logger.error("发布通道恢复失败，放弃发送")
                                return False
                    
                    # 不创建exchange，假设exchange已存在
                    
                    props = properties or pika.BasicProperties(
                        delivery_mode=2,
                        content_type="text/plain",
                        content_encoding="UTF-8"
                    )
                    
                    self.logger.debug(f"publish sending message exchange={exchange} routing_key={routing_key} message_length={len(message)}")
                    self.publisher_channel.basic_publish(
                        exchange=exchange,
                        routing_key=routing_key,
                        body=message,
                        properties=props
                    )
                    
                    # macOS 上强制刷新缓冲区（避免 pika 并发 bug）
                    if platform.system() == 'Darwin':
                        self.connection.process_data_events(time_limit=0.01)
                    
                    self.logger.debug(f"publish message sent successfully exchange={exchange} routing_key={routing_key}")
                    
                    if attempt > 1:
                        self.logger.info(f"publish_retry_success attempt={attempt}")
                    return True
                    
                except (pika.exceptions.AMQPError, 
                        ChannelWrongStateError,
                        AMQPChannelError,
                        ConnectionResetError, 
                        ConnectionError, 
                        OSError) as e:
                    error_type = type(e).__name__
                    self.logger.warning(
                        f"发送消息失败，尝试 {attempt}/{max_retries}, "
                        f"错误类型: {error_type}, 错误: {e}"
                    )
                    
                    if attempt < max_retries:
                        delay = retry_delay * attempt
                        self.logger.info(f"等待 {delay} 秒后重试...")
                        time.sleep(delay)
                        
                        if not self._recover_publisher():
                            self.logger.error("恢复连接失败")
                            continue
                    else:
                        self.logger.error(
                            f"达到最大重试次数({max_retries})，放弃发送。"
                            f"Exchange: {exchange}, RoutingKey: {routing_key}"
                        )
                        return False
                        
                except Exception as e:
                    self.logger.error(f"发送消息时发生意外异常: {e}")
                    if attempt >= max_retries:
                        return False
                    time.sleep(retry_delay * attempt)
            
            return False
    
    def consume_long_task(self, queue: str, callback: Callable[[bytes], bool], 
                         task_timeout: int = 3600, stop_event=None,
                         exchange: Optional[str] = None,
                         routing_key: Optional[str] = None) -> bool:
        """
        长任务消费（阻塞运行）
        
        Args:
            queue: 队列名称
            callback: 业务函数 Callable[[bytes], bool]
            task_timeout: 单个任务超时（秒）
            stop_event: 停止事件
            exchange: Exchange名称（可选，用于声明exchange和binding）
            routing_key: Routing Key（可选，用于绑定queue到exchange）
        
        Returns:
            bool: True=正常停止，False=异常退出
        """
        result_q = queue_module.Queue(maxsize=1)
        task_start_time = None
        processing_lock = threading.Lock()  # 防止设备并发操作

        def on_message(ch, method, properties, body):
            nonlocal task_start_time
            try:
                # 消息格式验证（基本检查）
                if len(body) < MIN_MESSAGE_SIZE:
                    self.logger.error(f"消息太短 length={len(body)} delivery_tag={method.delivery_tag}")
                    self._safe_nack_in_callback(ch, method.delivery_tag, requeue=False)
                    return
                
                # 验证是否为有效的 UTF-8
                try:
                    body.decode('utf-8')
                except UnicodeDecodeError:
                    self.logger.error(f"消息编码错误 delivery_tag={method.delivery_tag}")
                    self._safe_nack_in_callback(ch, method.delivery_tag, requeue=False)
                    return
                
                # 防止并发处理（理论上暂停消费者后不应该收到第二条消息）
                if not processing_lock.acquire(blocking=False):
                    self.logger.error(f"异常：收到新消息但锁被占用（不应发生）delivery_tag={method.delivery_tag}")
                    return
                
                # 暂停消费者（处理期间不接收新消息）
                self._pause_consumer()
                
                # 立即 ACK（避免重投）
                try:
                    ch.basic_ack(delivery_tag=method.delivery_tag)
                    self.logger.debug(f"✓ 立即ACK delivery_tag={method.delivery_tag}")
                except Exception as e:
                    self.logger.warning(f"ACK失败但继续执行 delivery_tag={method.delivery_tag} error={e}")
                
                task_start_time = time.time()
                threading.Thread(target=_worker, args=(body,), daemon=True).start()
            except Exception as e:
                self.logger.error(f"on_message异常 delivery_tag={method.delivery_tag} error={e}")
                self._safe_nack_in_callback(ch, method.delivery_tag, requeue=False)

        def _worker(body):
            try:
                ok = callback(body)
                result_q.put(("ok" if ok else "fail", body))
            except Exception as e:
                self.logger.error(f"worker处理异常 error={e}")
                result_q.put(("except", str(e)))
            finally:
                # 释放处理锁，允许下一个消息
                processing_lock.release()

        if not self._setup_consumer(queue, on_message, exchange, routing_key):
            self.logger.error(f"设置消费者失败 queue={queue} exchange={exchange}")
            return False

        retry_count = 0
        max_retries = self.config.get("connection_attempts", DEFAULT_CONNECTION_ATTEMPTS) * 2
        heartbeat = self.config.get("heartbeat", DEFAULT_HEARTBEAT)
        process_limit = self._calculate_process_limit(heartbeat)
        
        self.logger.info(f"开始长任务消费 queue={queue} heartbeat={heartbeat}s max_retries={max_retries}")
        
        while not (stop_event and stop_event.is_set()):
            try:
                # 处理事件（正常情况返回True，连接异常返回False）
                process_ok = self._process_events(time_limit=process_limit)
                
                # 只有在确认连接真的断开时才重连（避免误判）
                if not process_ok:
                    # 再次确认连接状态，避免误判
                    if self.is_connected():
                        self.logger.debug("process_events返回False但连接正常，继续运行")
                        # 连接正常，继续处理消息
                    else:
                        self.logger.warning("连接丢失，尝试重连")
                        
                        # 立即等待正在执行的任务完成（避免设备并发操作）
                        if processing_lock.locked():
                            self.logger.info("检测到任务正在执行，等待完成后再重连（避免设备冲突）...")
                            wait_start = time.time()
                            while processing_lock.locked() and (time.time() - wait_start < 300):
                                time.sleep(0.5)
                                if stop_event and stop_event.is_set():
                                    return True
                            
                            if processing_lock.locked():
                                self.logger.warning(f"等待任务完成超时(300s)，强制重连")
                            else:
                                self.logger.info("✓ 任务已完成，开始重连")
                        
                        retry_count += 1
                        if retry_count >= max_retries:
                            self.logger.error(f"重连失败 max_retries={max_retries}")
                            return False
                        
                        if self.reconnect(stop_event):
                            self.logger.info(f"✓ 重连成功 retry_count={retry_count}")
                            retry_count = 0
                            
                            # 清空结果队列（避免旧结果干扰）
                            while not result_q.empty():
                                try:
                                    result_q.get_nowait()
                                except queue_module.Empty:
                                    break
                            
                            # 重连后消费者已恢复，但如果锁还被占用说明任务还在执行
                            # 需要暂停消费者等任务完成
                            if processing_lock.locked():
                                self.logger.info("重连后任务仍在执行，暂停消费者")
                                self._pause_consumer()
                            
                            if not self._consumer_active and not processing_lock.locked():
                                self.logger.error("重连后消费者未激活且无任务执行，继续重连")
                                continue
                        else:
                            delay = min(self.reconnect_delay * (2 ** (retry_count - 1)), 30.0)
                            if self._wait_with_stop_check(delay, stop_event):
                                self.logger.info("等待期间收到停止信号")
                                return True
                            continue
                
                try:
                    status, _ = result_q.get_nowait()
                    self.logger.info(f"任务完成 status={status}")
                    task_start_time = None
                    
                    # 恢复消费者（继续接收新消息）
                    self._resume_consumer()
                    
                except queue_module.Empty:
                    if task_start_time and (time.time() - task_start_time > task_timeout):
                        self.logger.warning(f"任务超时 timeout={task_timeout}s（已ACK，强制释放锁）")
                        task_start_time = None
                        # 释放锁并恢复消费者
                        if processing_lock.locked():
                            processing_lock.release()
                            self.logger.info("✓ 超时任务锁已释放")
                        self._resume_consumer()
                    
            except (ConnectionClosed, AMQPConnectionError, ChannelWrongStateError, OSError, AssertionError) as e:
                error_type = type(e).__name__
                self.logger.warning(f"process_events连接异常 error_type={error_type} error={e}")
                self._mark_disconnected()
                retry_count += 1
                if retry_count >= max_retries:
                    self.logger.error(f"连接异常重试失败 max_retries={max_retries}")
                    return False
                delay = min(self.reconnect_delay * (2 ** (retry_count - 1)), 30.0)
                if self._wait_with_stop_check(delay, stop_event):
                    return True
                
            except Exception as e:
                self.logger.error(f"意外异常 error={e}")
                if self._wait_with_stop_check(1.0, stop_event):
                    return True

        self.logger.info(f"消费停止 queue={queue}")
        return True
    
    def reconnect(self, stop_event=None) -> bool:
        """完整重连"""
        retry_count = 0
        max_retries = self.config.get("connection_attempts", DEFAULT_CONNECTION_ATTEMPTS)
        
        self.logger.info(f"开始重连 max_retries={max_retries}")
        
        while retry_count < max_retries:
            if stop_event and stop_event.is_set():
                self.logger.info("重连期间收到停止信号")
                return False
                
            retry_count += 1
            self.logger.info(f"重连尝试 {retry_count}/{max_retries}")
            
            try:
                self._mark_disconnected()
                
                if self.connect():
                    # 验证连接是否真的可用
                    if not self.is_connected():
                        self.logger.warning(f"连接后验证失败 retry={retry_count}/{max_retries}")
                        continue
                    
                    self.logger.info("重连成功")
                    
                    # macOS 上稳定连接（避免 pika bug）
                    if platform.system() == 'Darwin':
                        time.sleep(0.1)
                    
                    # 如果有消费者配置，恢复消费者
                    if self._consumer_queue and self._consumer_callback:
                        self.logger.info(f"恢复消费者 queue={self._consumer_queue} exchange={self._consumer_exchange}")
                        if self._setup_consumer(self._consumer_queue, self._consumer_callback, 
                                               self._consumer_exchange, self._consumer_routing_key):
                            # 再次验证消费者是否设置成功
                            if self._consumer_active:
                                self.logger.info("消费者恢复成功")
                                return True
                            else:
                                self.logger.warning(f"消费者状态异常 retry={retry_count}/{max_retries}")
                                continue
                        else:
                            self.logger.warning(f"消费者恢复失败 retry={retry_count}/{max_retries}")
                            continue
                    else:
                        # 没有消费者配置，连接成功即可
                        return True
                    
            except Exception as e:
                self.logger.error(f"重连失败 error={e}")
            
            if retry_count >= max_retries:
                self.logger.error(f"重连失败，已达最大重试次数 {max_retries}")
                return False
            
            delay = min(self.reconnect_delay * (2 ** (retry_count - 1)), 30.0)
            self.logger.info(f"等待{delay:.1f}秒后重试")
            
            if self._wait_with_stop_check(delay, stop_event):
                self.logger.info("等待期间收到停止信号")
                return False
        
        return False
    
    def close(self):
        """关闭连接"""
        self.logger.info("关闭连接")
        try:
            # 先取消消费者
            if self._consumer_tag and self.consumer_channel and not self.consumer_channel.is_closed:
                try:
                    self.logger.debug(f"取消消费者 consumer_tag={self._consumer_tag}")
                    self.consumer_channel.basic_cancel(consumer_tag=self._consumer_tag)
                except Exception as e:
                    self.logger.debug(f"取消消费者失败 consumer_tag={self._consumer_tag} error={e}")
            
            self._consumer_active = False
            self._consumer_queue = None
            self._consumer_callback = None
            self._consumer_exchange = None
            self._consumer_routing_key = None
            self._consumer_tag = None
            self._consumer_meta = None
            
            self._cleanup_old_connection()
            self.logger.info("连接已关闭")
        except Exception as e:
            self.logger.warning(f"关闭连接异常 error={e}")
    
    # ------------- 内部方法 -------------
    
    def _recover_publisher(self) -> bool:
        """分级恢复发布通道"""
        connection_ok = (self.connection and 
                        not self.connection.is_closed and 
                        self.connection.is_open)
        
        if connection_ok:
            self.logger.info("只重建publisher_channel")
            try:
                if self.publisher_channel and not self.publisher_channel.is_closed:
                    try:
                        self.publisher_channel.close()
                    except Exception:
                        pass
                
                self.publisher_channel = self.connection.channel()
                self.publisher_channel.confirm_delivery()
                
                self.logger.info(f"publisher重建成功 channel={self.publisher_channel.channel_number}")
                return True
                
            except Exception as e:
                self.logger.warning(f"channel重建失败 error={e} 转完整重连")
                return self.reconnect()
        else:
            self.logger.info("connection异常 执行完整重连")
            return self.reconnect()
    
    def _setup_consumer(self, queue: str, callback: Callable, 
                       exchange: Optional[str] = None, 
                       routing_key: Optional[str] = None) -> bool:
        """设置消费者（支持声明exchange和binding，会先取消旧消费者）"""
        try:
            # 先取消旧的消费者（如果存在），避免重复创建
            if self._consumer_tag and self.consumer_channel and not self.consumer_channel.is_closed:
                try:
                    self.logger.debug(f"取消旧消费者 consumer_tag={self._consumer_tag}")
                    self.consumer_channel.basic_cancel(consumer_tag=self._consumer_tag)
                    self._consumer_tag = None
                except Exception as e:
                    self.logger.debug(f"取消旧消费者失败（可能已不存在） consumer_tag={self._consumer_tag} error={e}")
            
            self._consumer_queue = queue
            self._consumer_callback = callback
            self._consumer_exchange = exchange
            self._consumer_routing_key = routing_key
            
            # 保存订阅元数据（用于重连后恢复）
            self._consumer_meta = {
                "queue": queue,
                "exchange": exchange,
                "routing_key": routing_key
            }
            
            # 如果提供了exchange和routing_key，绑定queue到exchange（不创建队列和exchange，假设已存在）
            if exchange and routing_key:
                self.logger.debug(f"绑定queue到exchange exchange={exchange} queue={queue} routing_key={routing_key}")
                self.consumer_channel.queue_bind(exchange=exchange, queue=queue, routing_key=routing_key)
                self.logger.debug(f"exchange和queue绑定成功 exchange={exchange} queue={queue} routing_key={routing_key}")
            # 不创建队列，假设队列已存在
            
            self.consumer_channel.basic_qos(prefetch_count=1)
            # basic_consume 返回 consumer_tag
            # exclusive=True 确保只有一个消费者，防止多实例竞争
            self._consumer_tag = self.consumer_channel.basic_consume(
                queue=queue,
                on_message_callback=callback,
                auto_ack=False,
                exclusive=True
            )
            
            self._consumer_active = True
            self.logger.info(f"设置消费者成功 queue={queue} exchange={exchange or 'default'} consumer_tag={self._consumer_tag}")
            return True
            
        except Exception as e:
            self.logger.error(f"设置消费者失败 queue={queue} exchange={exchange} error={e}")
            self._consumer_active = False
            self._consumer_tag = None
            return False
    
    def _process_events(self, time_limit: float = 0.1) -> bool:
        """处理消费事件（返回True表示正常，False表示连接异常）"""
        try:
            # 先检查基本连接状态
            if not self.connection:
                return False
            if self.connection.is_closed:
                return False
            if not self.connection.is_open:
                return False
            if not self.consumer_channel or self.consumer_channel.is_closed:
                return False
            
            # 处理事件（正常情况会正常返回，即使没有消息）
            self.connection.process_data_events(time_limit=time_limit)
            return True
        except (ConnectionClosed, AMQPConnectionError, ChannelWrongStateError) as e:
            error_type = type(e).__name__
            self.logger.warning(f"process_events连接异常 error_type={error_type} error={e}")
            return False
        except Exception as e:
            # 其他异常可能是临时问题，记录但不一定表示连接断开
            self.logger.debug(f"process_events异常 error={e}")
            # 检查连接是否真的断开
            if not (self.connection and self.connection.is_open):
                return False
            # 如果连接状态正常，可能是其他问题，返回True继续运行
            return True
    
    def _safe_ack(self, delivery_tag: int) -> bool:
        """ACK确认"""
        if delivery_tag is None:
            return False
        
        if not (self.connection and not self.connection.is_closed):
            return False
        
        if not (self.consumer_channel and not self.consumer_channel.is_closed):
            return False
        
        try:
            self.consumer_channel.basic_ack(delivery_tag)
            return True
        except (ChannelWrongStateError, AMQPChannelError, ConnectionClosed, AMQPConnectionError) as e:
            error_type = type(e).__name__
            self.logger.error(f"ACK失败 delivery_tag={delivery_tag} error_type={error_type} error={e}")
            self._mark_disconnected()
            return False
        except Exception as e:
            self.logger.error(f"ACK失败 delivery_tag={delivery_tag} error={e}")
            self._mark_disconnected()
            return False
    
    def _safe_nack(self, delivery_tag: int, requeue: bool = False) -> bool:
        """NACK拒绝消息"""
        if delivery_tag is None:
            return False
        
        if not (self.connection and not self.connection.is_closed):
            return False
        
        if not (self.consumer_channel and not self.consumer_channel.is_closed):
            return False
        
        try:
            self.consumer_channel.basic_nack(delivery_tag=delivery_tag, requeue=requeue)
            return True
        except (ChannelWrongStateError, AMQPChannelError, ConnectionClosed, AMQPConnectionError) as e:
            error_type = type(e).__name__
            self.logger.error(f"NACK失败 delivery_tag={delivery_tag} requeue={requeue} error_type={error_type} error={e}")
            self._mark_disconnected()
            return False
        except Exception as e:
            self.logger.error(f"NACK失败 delivery_tag={delivery_tag} requeue={requeue} error={e}")
            self._mark_disconnected()
            return False
    
    def _cleanup_old_connection(self) -> None:
        """清理旧连接，防止连接泄漏"""
        try:
            # 清理旧的通道
            if self.consumer_channel and not self.consumer_channel.is_closed:
                try:
                    self.consumer_channel.close()
                    self.logger.debug("旧consumer_channel已关闭")
                except Exception as e:
                    self.logger.debug(f"关闭旧consumer_channel异常: {e}")
                finally:
                    self.consumer_channel = None
            
            if self.publisher_channel and not self.publisher_channel.is_closed:
                try:
                    self.publisher_channel.close()
                    self.logger.debug("旧publisher_channel已关闭")
                except Exception as e:
                    self.logger.debug(f"关闭旧publisher_channel异常: {e}")
                finally:
                    self.publisher_channel = None
            
            # 清理旧的连接
            if self.connection and not self.connection.is_closed:
                try:
                    self.connection.close()
                    self.logger.debug("旧connection已关闭")
                except Exception as e:
                    self.logger.debug(f"关闭旧connection异常: {e}")
                finally:
                    self.connection = None
            
            # 重置状态
            self._consumer_active = False
            self._consumer_tag = None
            
            self.logger.debug("旧连接清理完成")
            
        except Exception as e:
            self.logger.warning(f"清理旧连接异常: {e}")
    
    def _mark_disconnected(self):
        """标记断开并清理"""
        self._cleanup_old_connection()
    
    def _wait_with_stop_check(self, delay: float, stop_event) -> bool:
        """可中断等待（返回True表示收到停止信号）"""
        if not stop_event:
            time.sleep(delay)
            return False
        
        sleep_time = 0.0
        while sleep_time < delay:
            if stop_event.is_set():
                return True
            time.sleep(1.0)
            sleep_time += 1.0
        return False
    
    def _calculate_process_limit(self, heartbeat: int) -> float:
        """根据心跳计算事件处理时间限制"""
        if heartbeat >= 60:
            return 0.3
        elif heartbeat >= 30:
            return 0.2
        else:
            return 0.1
    
    def _safe_nack_in_callback(self, channel, delivery_tag: int, requeue: bool = False):
        """在回调中安全地 NACK 消息"""
        try:
            channel.basic_nack(delivery_tag=delivery_tag, requeue=requeue)
        except Exception as e:
            self.logger.error(f"NACK失败 delivery_tag={delivery_tag} error={e}")
    
    def _pause_consumer(self):
        """暂停消费者（停止接收新消息）"""
        try:
            if self._consumer_tag and self.consumer_channel and not self.consumer_channel.is_closed:
                self.consumer_channel.basic_cancel(consumer_tag=self._consumer_tag)
                self._consumer_active = False
                self.logger.debug(f"暂停消费者 consumer_tag={self._consumer_tag}")
        except Exception as e:
            self.logger.debug(f"暂停消费者失败 error={e}")
    
    def _resume_consumer(self):
        """恢复消费者（继续接收新消息）"""
        try:
            if self._consumer_queue and self._consumer_callback and not self._consumer_active:
                if self._setup_consumer(self._consumer_queue, self._consumer_callback, 
                                       self._consumer_exchange, self._consumer_routing_key):
                    self.logger.debug(f"恢复消费者 consumer_tag={self._consumer_tag}")
                else:
                    self.logger.warning("恢复消费者失败")
        except Exception as e:
            self.logger.warning(f"恢复消费者异常 error={e}")
    
    def check_consumer_health(self) -> dict:
        """检查消费者健康状态"""
        health_status = {
            "is_connected": self.is_connected(),
            "consumer_active": self._consumer_active,
            "has_consumer": bool(self._consumer_queue and self._consumer_callback),
            "consumer_tag": self._consumer_tag,
            "last_check_time": time.time()
        }
        
        return health_status
    
    def restart_consumer_if_needed(self) -> bool:
        """如果需要，重启消费者"""
        try:
            health = self.check_consumer_health()
            
            # 如果连接正常但消费者不活跃，且有消费者配置，尝试恢复
            if (health["is_connected"] and 
                not health["consumer_active"] and 
                health["has_consumer"] and
                self._consumer_meta):
                
                self.logger.warning("检测到消费者异常，正在恢复")
                queue = self._consumer_meta.get("queue")
                exchange = self._consumer_meta.get("exchange")
                routing_key = self._consumer_meta.get("routing_key")
                
                if queue and self._consumer_callback:
                    return self._setup_consumer(queue, self._consumer_callback, exchange, routing_key)
            
            return False
            
        except Exception as e:
            self.logger.error(f"检查消费者健康状态失败: {e}")
            return False
    
    def get_connection_stats(self) -> dict:
        """获取连接统计信息"""
        return {
            "is_connected": self.is_connected(),
            "consumer_active": self._consumer_active,
            "consumer_queue": self._consumer_queue
        }

