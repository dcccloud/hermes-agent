"""自然语言指令直接执行"""
from openclaw_agent.engine.core.operation import Operation, OperationResult, ExecutionContext


class NaturalLanguageOperation(Operation):
    """直接执行自然语言指令"""
    
    REQUIRED_PARAMS = ["prompt"]
    MAX_STEPS = 30
    
    def execute(self, context: ExecutionContext) -> OperationResult:
        agent = context.agent
        params = context.params
        
        prompt = params.get("prompt", "")
        if not prompt:
            return self.failed("提示词不能为空")
        
        max_steps = params.get("max_steps", self.MAX_STEPS)
        
        self.logger.info(f"执行自然语言指令 (max_steps={max_steps}): {prompt[:100]}...")
        
        try:
            original_max_steps = agent.agent_config.max_steps
            agent.agent_config.max_steps = max_steps
            
            result_message = agent.run(prompt)
            
            agent.agent_config.max_steps = original_max_steps
            
            self.logger.info(f"Agent 执行结果: {result_message}")
            
            result_lower = result_message.lower()
            
            if any(keyword in result_lower for keyword in ["失败", "错误", "未找到", "无法", "找不到", "max steps"]):
                return self.failed(
                    error=f"执行失败: {result_message}",
                    data={"agent_result": result_message, "max_steps": max_steps}
                )
            
            return self.success({"agent_result": result_message, "max_steps": max_steps})
        
        except Exception as e:
            self.logger.error(f"执行异常: {e}")
            return self.failed(f"执行异常: {str(e)}")
