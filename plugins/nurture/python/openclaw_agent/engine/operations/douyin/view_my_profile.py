"""打开抖音「我」页面并用纯 u2 采集 profile 数据（高频操作，不依赖 VLM）"""
import re
import time
from openclaw_agent.engine.core.operation import OperationResult, ExecutionContext
from openclaw_agent.engine.operations.douyin.base import DouyinBaseOperation
from openclaw_agent.engine.utils.interaction import safe_element_click


def _parse_count(text: str) -> int:
    """将 '1.2万' / '12.3w' / '1,234' 等文本解析为整数"""
    if not text:
        return 0
    text = text.strip().replace(",", "").replace(" ", "")
    m = re.match(r"([\d.]+)\s*[万wW]", text)
    if m:
        return int(float(m.group(1)) * 10_000)
    m = re.match(r"([\d.]+)\s*[亿]", text)
    if m:
        return int(float(m.group(1)) * 100_000_000)
    m = re.match(r"[\d.]+", text)
    if m:
        return int(float(m.group(0)))
    return 0


class ViewMyProfileOperation(DouyinBaseOperation):
    """打开抖音底部「我」tab，纯 u2 采集 profile 核心指标"""

    MAX_STEPS = 10

    def execute(self, context: ExecutionContext) -> OperationResult:
        device = context.device
        agent = context.agent

        if not self._navigate_to_me(device, agent):
            return self.failed("无法进入「我」页面")

        profile = self._scrape_with_u2(device)
        self.logger.info(f"profile 采集完成: {profile}")
        return self.success(data=profile)

    # ------------------------------------------------------------------
    # 导航：确保从任何页面状态都能到达「我」页面
    # 策略：已在 → 直接用 | 点 tab → 按返回退回 → 重启 app
    # ------------------------------------------------------------------

    def _navigate_to_me(self, device, agent) -> bool:
        # 0) 已经在「我」页面
        if self._verify_me_page(device):
            self.logger.info("已在「我」页面，跳过导航")
            return True

        # 1) 尝试直接点底部「我」tab（导航栏可见时）
        if self._try_click_me_tab(device):
            return True

        # 2) 可能在深层页面（导航栏隐藏），连按返回退到主页再点
        self.logger.info("底部导航栏不可见，尝试按返回退回主页")
        for i in range(5):
            device.press("back")
            time.sleep(0.8)
            if self._try_click_me_tab(device):
                return True

        # 3) 按返回也不行，重启抖音（落在 feed 流，导航栏必定可见）
        self.logger.info("按返回无效，重启抖音")
        if self.ensure_app_running(device):
            time.sleep(2.0)
            if self._try_click_me_tab(device):
                return True

        # 4) 最后 agent 原子兜底
        try:
            agent.run(
                "点击抖音底部导航栏最右侧的「我」tab。"
                "完成后回复「完成」。"
            )
            time.sleep(2.0)
            if self._verify_me_page(device):
                self.logger.info("已进入「我」页面 (agent)")
                return True
        except Exception as e:
            self.logger.error(f"agent 导航异常: {e}")

        return False

    def _try_click_me_tab(self, device) -> bool:
        """尝试用 u2 点击底部「我」tab"""
        for selector in [
            device(text="我", className="android.widget.TextView"),
            device(descriptionMatches="^我$"),
        ]:
            if selector.exists(timeout=1):
                if safe_element_click(selector, timeout=3, stable_time=0.3):
                    self.logger.info("已点击底部「我」tab (u2)")
                    time.sleep(2.0)
                    if self._verify_me_page(device):
                        return True
        return False

    def _verify_me_page(self, device) -> bool:
        """验证是否在「我」页面：关注/粉丝/获赞 三选一"""
        for text in ["关注", "粉丝", "获赞"]:
            if device(text=text).exists(timeout=1):
                return True
        return False

    # ------------------------------------------------------------------
    # 纯 u2 采集 profile 数据
    # ------------------------------------------------------------------

    def _scrape_with_u2(self, device) -> dict:
        profile: dict = {
            "nickname": None,
            "douyinId": None,
            "followers": None,
            "following": None,
            "likes_received": None,
            "works": None,
        }

        # 收集所有 TextView 的文本和位置
        elements = self._collect_text_elements(device)

        # 1) 提取统计指标：通过 label 位置找上方对应的数字
        label_map = {
            "关注": "following",
            "互关": "following",
            "粉丝": "followers",
            "获赞": "likes_received",
        }

        for elem in elements:
            field = label_map.get(elem["text"])
            if not field:
                continue
            # 找 label 正上方、水平对齐、最近的数字元素
            number = self._find_number_above(elements, elem)
            if number is not None:
                profile[field] = number
                self.logger.debug(f"  {elem['text']} = {number}")

        # 2) 抖音号
        for elem in elements:
            m = re.match(r"抖音号[：:]\s*(.+)", elem["text"])
            if m:
                profile["douyinId"] = m.group(1).strip()
                break

        # 3) 昵称：通常是抖音号上方的大字
        profile["nickname"] = self._find_nickname(elements, profile.get("douyinId"))

        # 4) 作品数（tab 文字 "作品 N" 或 "作品\nN"）
        for elem in elements:
            m = re.match(r"作品\s*(\d+)", elem["text"])
            if m:
                profile["works"] = int(m.group(1))
                break

        return profile

    def _collect_text_elements(self, device) -> list[dict]:
        """收集所有 TextView 元素的文本和边界"""
        elements = []
        for elem in device(className="android.widget.TextView"):
            try:
                text = elem.get_text()
                if not text or not text.strip():
                    continue
                bounds = elem.info.get("bounds", {})
                elements.append({
                    "text": text.strip(),
                    "cx": (bounds.get("left", 0) + bounds.get("right", 0)) // 2,
                    "top": bounds.get("top", 0),
                    "bottom": bounds.get("bottom", 0),
                    "left": bounds.get("left", 0),
                    "right": bounds.get("right", 0),
                })
            except Exception:
                continue
        return elements

    def _find_number_above(self, elements: list[dict], label_elem: dict, x_tolerance: int = 60) -> int | None:
        """在 label 元素正上方找到最近的数字文本"""
        best_val = None
        best_dist = float("inf")

        for e in elements:
            # 必须在 label 上方
            if e["bottom"] > label_elem["top"]:
                continue
            # 水平对齐（中心点偏差在容忍范围内）
            if abs(e["cx"] - label_elem["cx"]) > x_tolerance:
                continue
            # 必须是数字格式
            if not re.match(r"^[\d,.]+[万亿wW]?$", e["text"]):
                continue
            dist = label_elem["top"] - e["bottom"]
            if dist < best_dist:
                best_dist = dist
                best_val = _parse_count(e["text"])

        return best_val

    def _find_nickname(self, elements: list[dict], douyin_id: str | None) -> str | None:
        """昵称通常在抖音号上方、页面上部的显眼文字"""
        if not douyin_id:
            return None

        # 找到抖音号元素的位置
        id_elem = None
        for e in elements:
            if douyin_id in e["text"]:
                id_elem = e
                break
        if not id_elem:
            return None

        # 在抖音号上方找最近的非功能性文字（排除常见干扰）
        skip_patterns = [
            r"^(关注|粉丝|获赞|互关|作品|抖音号|IP|编辑|设置).*",
            r"^\d+$",
            r"^[\d,.]+[万亿wW]?$",
        ]
        best = None
        best_dist = float("inf")

        for e in elements:
            if e["bottom"] > id_elem["top"]:
                continue
            if any(re.match(p, e["text"]) for p in skip_patterns):
                continue
            if len(e["text"]) < 1 or len(e["text"]) > 30:
                continue
            dist = id_elem["top"] - e["bottom"]
            if dist < best_dist:
                best_dist = dist
                best = e["text"]

        return best
