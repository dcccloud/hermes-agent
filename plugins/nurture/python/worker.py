"""
OperationEngine 主程序

使用方式：
  python main.py dev   - 本地开发环境
  python main.py test  - 生产MQ测试队列（不影响生产）
  python main.py prod  - 生产环境
"""

import sys
import os
import signal

# 全局变量（用于信号处理）
consumer = None
logger = None


def signal_handler(sig, frame):
    """信号处理器（优雅停止）"""
    global consumer, logger
    
    signal_name = "SIGINT" if sig == signal.SIGINT else "SIGTERM"
    logger.info(f"收到信号 {signal_name}，开始优雅停止...")
    
    if consumer:
        consumer.stop()
    
    logger.info("停止信号已发送，等待任务完成...")


def main():
    global consumer, logger
    
    # 设置环境（必须在导入其他模块之前）
    # 支持：dev, test, prod
    env = sys.argv[1] if len(sys.argv) > 1 else "dev"
    os.environ['APP_ENV'] = env
    
    # 导入其他模块（确保 APP_ENV 已设置）
    from openclaw_agent.engine.common import get_logger, get_config
    from openclaw_agent.engine.agents.phone_agent_pool import PhoneAgentPool
    from openclaw_agent.engine.core.executor import OperationExecutor
    from openclaw_agent.engine.mq.consumer import TaskConsumer
    import openclaw_agent.engine.operations
    
    # 加载配置（重新加载以确保使用正确的环境）
    from openclaw_agent.engine.common.config import reload_config
    config = reload_config()
    logger = get_logger("main")
    
    logger.info(f"OperationEngine 启动中... 环境: {env}")
    logger.info(f"MQ地址: {config.mq.host}:{config.mq.port}")
    logger.info(f"消费队列: {config.mq.queue}")
    logger.info(f"结果队列: {config.mq.actions_response_queue}")
    
    try:
        # 初始化数据库（暂时注释，后续启用）
        # db_url = f"mysql+pymysql://{config.db.username}:{config.db.password}@{config.db.host}:{config.db.port}/{config.db.database}"
        # repository.operation_repository = OperationRepository(db_url)
        # logger.info("数据库连接成功")
        
        # 初始化 PhoneAgent 池
        agent_pool = PhoneAgentPool(
            base_url=config.autoglm.base_url,
            api_key=config.autoglm.api_key,
            model_name=config.autoglm.model_name,
            max_steps=config.autoglm.max_steps,
            temperature=config.autoglm.temperature
        )
        logger.info("PhoneAgent 池初始化成功")
        
        # 初始化执行器
        executor = OperationExecutor(agent_pool)
        logger.info("Operation 执行器初始化成功")
        
        # 初始化消费者
        consumer = TaskConsumer(config.mq.model_dump())
        consumer.connect()
        logger.info("RabbitMQ 消费者连接成功")
        
        # 注册信号处理器（优雅停止）
        signal.signal(signal.SIGINT, signal_handler)   # Ctrl+C
        signal.signal(signal.SIGTERM, signal_handler)  # kill 命令
        logger.info("信号处理器注册成功")
        
        logger.info("=" * 50)
        logger.info("OperationEngine 启动成功！")
        logger.info("=" * 50)
        
        # 开始消费（阻塞）
        def execute_with_callback(message, op_callback):
            """包装执行函数，传入 operation callback"""
            return executor.execute_task(message, op_callback)
        
        consumer.start_consuming(execute_with_callback)
        
    except KeyboardInterrupt:
        logger.info("收到键盘中断")
    except Exception as e:
        logger.error(f"启动失败: {e}", exc_info=True)
        sys.exit(1)
    finally:
        # 确保资源清理
        if consumer:
            logger.info("清理资源...")
            consumer.close()
        logger.info("OperationEngine 已停止")


if __name__ == "__main__":
    main()
