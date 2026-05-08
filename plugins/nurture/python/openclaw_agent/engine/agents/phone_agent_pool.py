import threading
from typing import Dict


class PhoneAgentPool:
    """PhoneAgent 实例池，缓存实例避免重复初始化"""
    
    def __init__(self, base_url: str, api_key: str, model_name: str, max_steps: int = 50, temperature: float = 0.0):
        self.base_url = base_url
        self.api_key = api_key
        self.model_name = model_name
        self.default_max_steps = max_steps
        self.temperature = temperature
        self._agents: Dict[str, any] = {}
        self._lock = threading.Lock()
    
    def get(self, device_id: str, reset_context: bool = True, max_steps: int = None):
        """
        获取 PhoneAgent 实例
        
        :param device_id: 设备ID
        :param reset_context: 是否重置上下文
        :param max_steps: 自定义最大步数（None则使用默认值）
        """
        with self._lock:
            # 使用 max_steps 或默认值
            actual_max_steps = max_steps if max_steps is not None else self.default_max_steps
            
            # 为每个 (device_id, max_steps) 组合创建独立的 agent 实例
            cache_key = f"{device_id}_{actual_max_steps}"
            
            if cache_key not in self._agents:
                from openclaw_agent.device import PhoneAgent
                from openclaw_agent.device.model import ModelConfig
                from openclaw_agent.device.agent import AgentConfig
                
                model_config = ModelConfig(
                    base_url=self.base_url,
                    api_key=self.api_key,
                    model_name=self.model_name,
                    temperature=self.temperature
                )
                agent_config = AgentConfig(
                    device_id=device_id,
                    max_steps=actual_max_steps
                )
                
                self._agents[cache_key] = PhoneAgent(
                    model_config=model_config,
                    agent_config=agent_config
                )
            
            agent = self._agents[cache_key]
            if reset_context:
                agent.reset()
            
            return agent
    
    def remove(self, device_id: str):
        """移除 PhoneAgent 实例"""
        with self._lock:
            if device_id in self._agents:
                del self._agents[device_id]
