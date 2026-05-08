# Operations 开发指南

## 目录结构

```
operations/
├── douyin/           # 抖音操作
│   ├── base.py       # DouyinBaseOperation
│   └── *.py          # 具体操作实现
├── haosheng/         # 好省短剧操作
│   ├── base.py       # HaoshengBaseOperation
│   └── *.py          # 具体操作实现
├── xingtu/           # 星图小程序操作
│   ├── base.py       # XingtuBaseOperation
│   └── *.py          # 具体操作实现
└── wechat/           # 微信操作
```

## 继承层级

```
Operation (基类，包含通用方法)
    ├── DouyinBaseOperation (抖音专属)
    │   └── GiveALikeOperation (具体实现)
    ├── HaoshengBaseOperation (好省专属)
    │   └── PlayShortDramaOperation (具体实现)
    └── XingtuBaseOperation (星图专属)
        └── PublishDramaOperation (具体实现)
```

## Operation 基类提供的通用方法

### 元素操作

- `wait_element(device, selector, timeout)` - 等待元素出现
- `find_and_click(device, selector, timeout)` - 查找并点击元素
- `safe_click(device, x, y, retry)` - 安全点击坐标
- `check_element_exists(device, selector)` - 检查元素是否存在
- `get_text(device, selector)` - 获取元素文本

### 手势操作

- `swipe_up(device, duration)` - 向上滑动
- `swipe_down(device, duration)` - 向下滑动
- `wait_page_load(duration)` - 等待页面加载

### 结果返回

- `success(data)` - 返回成功结果
- `failed(error, data)` - 返回失败结果
- `skipped(reason)` - 返回跳过结果

## App 专属 BaseOperation 提供的方法

### DouyinBaseOperation

- `ensure_app_running(device)` - 确保抖音正在运行
- `click_tab(device, tab_name)` - 点击底部 Tab
- `enter_my_page(device)` - 进入"我"页面
- `is_video_playing(device)` - 判断是否在播放视频

### HaoshengBaseOperation

- `ensure_app_running(device)` - 确保好省短剧正在运行
- `click_tab(device, tab_name)` - 点击底部 Tab
- `is_drama_playing(device)` - 判断是否在播放短剧
- `enter_my_page(device)` - 进入我的页面

### XingtuBaseOperation

- `ensure_in_miniapp(device)` - 确保在星图小程序内
- `click_tab(device, tab_name)` - 点击小程序内 Tab

## 开发新 Operation 示例

### 1. 继承 App 专属基类

```python
from openclaw_agent.engine.core.operation import OperationDefinition, OperationResult, ExecutionContext
from openclaw_agent.engine.core.registry import registry
from openclaw_agent.engine.operations.douyin.base import DouyinBaseOperation


@registry.register
class GiveALikeOperation(DouyinBaseOperation):
    """点赞操作"""

    def define(self) -> OperationDefinition:
        return OperationDefinition(
            name="give_a_like",
            display_name="点赞",
            app="douyin",
            parameters=[],
            preconditions=[]
        )

    def execute(self, context: ExecutionContext) -> OperationResult:
        device = context.device

        # 使用基类方法
        if not self.ensure_app_running(device):
            return self.failed("抖音未运行")

        # 查找并点击点赞按钮
        if self.find_and_click(device, {"text": "点赞"}, timeout=5):
            self.wait_page_load(0.5)
            return self.success({"action": "点赞成功"})

        return self.failed("未找到点赞按钮")
```

### 2. 添加自定义辅助方法

```python
class MyOperation(DouyinBaseOperation):

    def _check_login_status(self, device) -> bool:
        """检查登录状态（自定义方法）"""
        return self.check_element_exists(device, {"text": "我的"})

    def execute(self, context: ExecutionContext) -> OperationResult:
        device = context.device

        if not self._check_login_status(device):
            return self.failed("用户未登录")

        # 执行业务逻辑
        return self.success()
```

### 3. 复用 utils 和 agent

```python
def execute(self, context: ExecutionContext) -> OperationResult:
    device = context.device
    agent = context.agent
    utils = context.utils

    # 方式1：使用基类方法
    self.find_and_click(device, {"text": "素材"})

    # 方式2：使用 utils
    utils.tap.safe_tap(device, "素材")

    # 方式3：使用 agent
    agent.chat("帮我点击素材标签")

    # 方式4：直接使用 u2
    device(text="素材").click()

    return self.success()
```

## 最佳实践

1. **优先继承 App 专属基类**：如 `DouyinBaseOperation`
2. **使用基类提供的方法**：减少重复代码
3. **合理使用日志**：`self.logger.info/warning/error`
4. **统一返回结果**：使用 `self.success()` / `self.failed()` / `self.skipped()`
5. **添加辅助方法**：将复杂逻辑拆分为私有方法
6. **处理异常**：基类方法已处理大部分异常，特殊情况需额外处理
