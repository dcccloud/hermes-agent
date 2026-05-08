"""
消息队列消费者

基于 RabbitMQClient 的任务消费者
"""
import json
import threading
import contextvars
from typing import Callable, Dict, Any
from openclaw_agent.engine.common import get_logger, set_task_id, clear_task_id, get_task_id
from openclaw_agent.engine.mq.rabbitmq_client import RabbitMQClient


logger = get_logger("mq.consumer")


class TaskConsumer:
    """任务消息消费者"""
    
    def __init__(self, mq_config: Dict[str, Any]):
        """
        初始化消费者
        
        Args:
            mq_config: MQ配置字典
        """
        self.mq_config = mq_config
        self.client = None
        self._stop_event = threading.Event()
        self._consuming = False
    
    def connect(self):
        """连接消息队列"""
        logger.info("初始化RabbitMQ客户端")
        self.client = RabbitMQClient(config=self.mq_config, logger=logger)
        
        if not self.client.connect():
            raise ConnectionError("RabbitMQ连接失败")
        
        logger.info("RabbitMQ连接成功")
    
    def start_consuming(self, callback: Callable[[Dict[str, Any], Callable], Dict[str, Any]]):
        """
        开始消费消息（阻塞运行）
        
        Args:
            callback: 业务回调函数 Callable[[Dict, Callable], Dict]
                     接收任务消息和 operation 结果发布函数，返回执行结果
        """
        if not self.client:
            raise RuntimeError("请先调用 connect() 连接消息队列")
        
        queue = self.mq_config["queue"]
        task_timeout = self.mq_config.get("task_timeout", 3600)
        
        logger.info(f"开始消费队列: {queue}, 任务超时: {task_timeout}s")
        self._consuming = True
        
        def on_message(body: bytes) -> bool:
            """消息处理回调"""
            task_id = 'unknown'
            task_type = 'unknown'
            
            try:
                # 解析消息（嵌套格式）
                message = json.loads(body.decode('utf-8'))
                
                # 打印收到的消息
                logger.info(f"收到任务消息: {json.dumps(message, ensure_ascii=False)}")
                
                # 提取业务数据
                task_data = message['data']
                task_id = task_data.get('task_id', 'unknown')
                task_type = task_data.get('task_type', 'unknown')
                
                # 设置当前任务的 task_id 到日志上下文
                set_task_id(task_id)
                
                # 执行业务逻辑，传入 operation 结果发布函数
                result = callback(task_data, self._publish_operation_result)
                
                # 发布最终结果到结果队列
                self._publish_result(result)
                
                logger.info(f"任务处理完成 success={result.get('success')}")
                
                # 清除 task_id（必须在所有日志打印之后）
                clear_task_id()
                
                return True
                
            except json.JSONDecodeError as e:
                logger.error(f"消息JSON格式错误: {e}")
                # 回复失败结果
                self._publish_result({
                    'task_id': task_id,
                    'task_type': task_type,
                    'success': False,
                    'result': {
                        'error': f'消息JSON格式错误: {str(e)}',
                        'device_id': 'unknown',
                        'operations_result': []
                    }
                })
                clear_task_id()
                return False
                
            except KeyError as e:
                logger.error(f"消息格式错误，缺少必要字段: {e}", exc_info=True)
                # 回复失败结果
                self._publish_result({
                    'task_id': task_id,
                    'task_type': task_type,
                    'success': False,
                    'result': {
                        'error': f'消息格式错误，缺少必要字段: {str(e)}',
                        'device_id': 'unknown',
                        'operations_result': []
                    }
                })
                clear_task_id()
                return False
                
            except Exception as e:
                logger.error(f"处理消息异常: {e}", exc_info=True)
                # 回复失败结果
                self._publish_result({
                    'task_id': task_id,
                    'task_type': task_type,
                    'success': False,
                    'result': {
                        'error': f'处理消息异常: {str(e)}',
                        'device_id': 'unknown',
                        'operations_result': []
                    }
                })
                clear_task_id()
                return False
        
        try:
            # 使用长任务消费模式
            success = self.client.consume_long_task(
                queue=queue,
                callback=on_message,
                task_timeout=task_timeout,
                stop_event=self._stop_event
            )
            
            if not success:
                logger.error("消费异常退出")
            else:
                logger.info("消费正常停止")
        finally:
            self._consuming = False
    
    def _publish_operation_result(self, op_result: Dict[str, Any]):
        """发布单个 operation 的执行结果到 actions.response 队列"""
        try:
            actions_response_queue = self.mq_config.get("actions_response_queue")
            if not actions_response_queue:
                logger.warning("未配置 actions_response_queue，跳过 operation 结果发布")
                return
            
            result_json = json.dumps(op_result, ensure_ascii=False)
            
            # 打印要发送的 operation 结果消息（简化大字段）
            log_result = op_result.copy()
            if 'result' in log_result and isinstance(log_result['result'], dict):
                result_data = log_result['result'].copy()
                if 'ui_xml' in result_data:
                    result_data['ui_xml'] = f"<XML data: {len(result_data['ui_xml'])} bytes>"
                if 'agent_result' in result_data and len(result_data['agent_result']) > 200:
                    result_data['agent_result'] = result_data['agent_result'][:200] + "..."
                log_result['result'] = result_data
            
            logger.info(f"发送 Operation 结果到 {actions_response_queue}: {json.dumps(log_result, ensure_ascii=False)}")
            
            success = self.client.publish(
                exchange="",
                routing_key=actions_response_queue,
                message=result_json
            )
            
            if success:
                logger.debug(f"✓ Operation 结果已发布")
            else:
                logger.error(f"✗ Operation 结果发布失败: {op_result.get('op_name')}")
                
        except Exception as e:
            logger.error(f"发布 operation 结果异常: {e}")
    
    def _publish_result(self, result: Dict[str, Any]):
        """发布最终任务结果到 observations 队列"""
        try:
            observations_queue = self.mq_config.get("observations_queue")
            if not observations_queue:
                logger.warning("未配置 observations_queue，跳过最终结果发布")
                return
            
            result_json = json.dumps(result, ensure_ascii=False)
            
            # 打印要发送的最终结果消息（简化大字段）
            log_result = result.copy()
            if 'result' in log_result and 'operations_result' in log_result['result']:
                ops = []
                for op in log_result['result']['operations_result']:
                    op_copy = op.copy()
                    if 'data' in op_copy and isinstance(op_copy['data'], dict):
                        data_copy = op_copy['data'].copy()
                        if 'ui_xml' in data_copy:
                            data_copy['ui_xml'] = f"<XML data: {len(data_copy['ui_xml'])} bytes>"
                        if 'agent_result' in data_copy and len(data_copy['agent_result']) > 200:
                            data_copy['agent_result'] = data_copy['agent_result'][:200] + "..."
                        op_copy['data'] = data_copy
                    ops.append(op_copy)
                log_result['result']['operations_result'] = ops
            
            result_json_pretty = json.dumps(log_result, ensure_ascii=False, indent=2)
            logger.info(f"发送最终结果到 {observations_queue}:\n{result_json_pretty}")
            
            success = self.client.publish(
                exchange="",
                routing_key=observations_queue,
                message=result_json
            )
            
            if success:
                logger.info(f"✓ 最终结果已发布到 {observations_queue}")
            else:
                logger.error(f"✗ 最终结果发布失败")
                
        except Exception as e:
            logger.error(f"发布最终结果异常: {e}")
    
    def stop(self):
        """停止消费（设置停止信号）"""
        if self._consuming:
            logger.info("设置停止信号...")
            self._stop_event.set()
        else:
            logger.info("消费者未运行，无需停止")
    
    def close(self):
        """关闭连接（确保资源清理）"""
        logger.info("关闭消费者连接")
        try:
            if self.client:
                self.client.close()
        except Exception as e:
            logger.error(f"关闭连接异常: {e}")
    
    def is_consuming(self) -> bool:
        """检查是否正在消费"""
        return self._consuming
