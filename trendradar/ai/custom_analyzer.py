# coding=utf-8
"""
定制 NLP 分析模块（芯朋微情报体系建设）

对 TrendRadar 爬取的新闻条目进行结构化 NLP 分析，包括：
1. 情感分析（正面/负面/中性 + 0-1 分值）
2. 影响评估（影响维度 + 1-5 评分）
3. 风险等级判定（L1-L5）
4. PEST 归类
5. 关联企业识别
6. 技术标签匹配

复用 TrendRadar 已有的 LiteLLM 客户端。
"""

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, Union

from trendradar.ai.client import AIClient
from trendradar.storage.base import NewsItem, RSSItem


# === LiteLLM 模型配置 ===
# 通过环境变量覆盖，默认使用 DeepSeek（性价比高，适合批量分析）
DEFAULT_MODEL = os.environ.get("NLP_MODEL", "deepseek/deepseek-chat")
DEFAULT_API_KEY = os.environ.get("NLP_API_KEY", "")
DEFAULT_API_BASE = os.environ.get("NLP_API_BASE", "")


# 技术关键词匹配表（无 LLM 调用时的兜底方案）
TECH_KEYWORDS = {
    "GaN": ["GaN", "氮化镓", "gallium nitride"],
    "SiC": ["SiC", "碳化硅", "silicon carbide", "sic"],
    "BCD": ["BCD", "bcd", "Bipolar-CMOS-DMOS"],
    "AC-DC": ["AC-DC", "ac-dc", "acdc", "ACDC", "交流-直流", "电源转换"],
    "DC-DC": ["DC-DC", "dc-dc", "dcdc", "DCDC", "直流-直流"],
    "PMIC": ["PMIC", "pmic", "电源管理", "power management"],
    "LDO": ["LDO", "ldo", "低压差"],
    "MOSFET": ["MOSFET", "mosfet", "Mosfet", "功率管", "场效应管"],
    "Driver": ["Driver", "driver", "驱动", "栅极驱动"],
    "Analog": ["Analog", "analog", "模拟", "模拟芯片"],
    "Signal Chain": ["信号链", "signal chain", "放大器", "比较器", "ADC", "DAC", "转换器"],
    "功率器件": ["功率器件", "功率半导体", "IGBT", "power device"],
}

COMPANY_PATTERNS = {
    "芯朋微": ["芯朋微", "chipown", "Chipown"],
    "圣邦微": ["圣邦微", "sgmicro", "SGMICRO"],
    "思瑞浦": ["思瑞浦", "3PEAK", "3peak"],
    "纳芯微": ["纳芯微", "NOVOSENSE", "novosense"],
    "艾为电子": ["艾为", "awinic", "AWINIC"],
    "力芯微": ["力芯微", "etic", "ETIC"],
    "希荻微": ["希荻微", "HALO", "halo micro"],
    "晶丰明源": ["晶丰明源", "bright", "Bright Power"],
    "富满电子": ["富满", "FM", "富满微"],
    "明微电子": ["明微", "明微电子"],
    "TI": ["TI ", "德州仪器", "Texas Instruments", "ti.com"],
    "ADI": ["ADI", "亚德诺", "Analog Devices", "analog.com"],
    "英飞凌": ["英飞凌", "Infineon", "infineon"],
    "安森美": ["安森美", "onsemi", "ON Semiconductor"],
    "意法半导体": ["意法半导体", "STMicro", "ST "],
}

PEST_KEYWORDS = {
    "政治": ["政策", "法规", "监管", "出口管制", "制裁", "补贴", "大基金", "贸易", "关税", "政府",
             "license", "许可证", "合规", "中美", "禁令", "白名单", "实体清单"],
    "经济": ["市场", "需求", "价格", "库存", "周期", "营收", "利润", "毛利率", "经济", "GDP",
             "汇率", "通货膨胀", "降息", "涨价", "降价", "出货量", "产能"],
    "社会": ["人才", "就业", "教育", "人口", "消费", "品牌", "国产替代", "自主可控", "供应链安全"],
    "技术": ["技术", "工艺", "制程", "BCD", "GaN", "SiC", "专利", "研发", "创新", "突破",
             "IP", "设计", "流片", "封装", "测试", "晶圆", "纳米"],
}


@dataclass
class NLPAnalysis:
    """NLP 单条分析结果"""
    sentiment: str = ""              # 情感倾向：正面/负面/中性
    sentiment_score: float = 0.0     # 情感分值 0-1
    impact_dimension: str = ""       # 影响维度：正面影响/负面影响/中性/不确定
    impact_score: int = 0            # 影响评分 1-5
    risk_level: str = ""             # 风险等级：L1轻微/L2关注/L3预警/L4严重/L5危机
    pest_dimension: str = ""         # PEST维度：政治/经济/社会/技术/综合
    pest_analysis: str = ""          # PEST分析简述
    tech_tags: List[str] = None      # 技术标签
    companies: List[str] = None      # 关联企业
    confidence: float = 0.0          # 分析置信度


class CustomNewsAnalyzer:
    """
    定制新闻 NLP 分析器

    对单条新闻进行情感/影响/风险/PEST 等多维度分析。
    优先调用 LLM 进行深度分析，兜底使用关键词匹配。
    """

    def __init__(self, ai_config: Optional[Dict[str, Any]] = None):
        """
        初始化分析器

        Args:
            ai_config: AI 客户端配置字典（可选）
                       不传则尝试从环境变量读取
        """
        if ai_config:
            self.client = AIClient(ai_config) if ai_config.get("API_KEY") else None
        elif DEFAULT_API_KEY:
            self.client = AIClient({
                "MODEL": DEFAULT_MODEL,
                "API_KEY": DEFAULT_API_KEY,
                "API_BASE": DEFAULT_API_BASE,
                "TEMPERATURE": 0.1,
                "MAX_TOKENS": 512,
                "TIMEOUT": 15,
            })
        else:
            self.client = None

    def analyze(self, title: str, content: str = "") -> NLPAnalysis:
        """
        分析单条新闻

        Args:
            title: 新闻标题
            content: 新闻正文/摘要（可选）

        Returns:
            NLPAnalysis 分析结果
        """
        if self.client:
            return self._analyze_with_llm(title, content)
        return self._analyze_with_rules(title, content)

    def _build_prompt(self, title: str, content: str) -> str:
        """构造分析提示词"""
        text = f"标题：{title}"
        if content:
            text += f"\n正文：{content[:2000]}"

        return f"""你是一个专业的半导体行业舆情分析师。请分析以下新闻，返回严格的 JSON 格式（不要多余文字）。

新闻信息：
{text}

请分析并返回以下 JSON（字段说明见下方）：
{{
    "sentiment": "正面|负面|中性",         // 情感倾向
    "sentiment_score": 0.75,               // 情感分值 0-1（0极负面, 0.5中性, 1极正面）
    "impact_dimension": "正面影响|负面影响|中性|不确定",  // 对芯朋微的影响维度
    "impact_score": 3,                     // 对芯朋微的影响评分 1-5（1最低, 5最高）
    "risk_level": "L1轻微|L2关注|L3预警|L4严重|L5危机",  // 风险等级
    "pest_dimension": "政治|经济|社会|技术|综合",         // PEST 维度
    "pest_analysis": "简述该新闻所属的PEST维度判断依据",   // 一句话说明
    "tech_tags": ["GaN", "AC-DC"],         // 技术标签，从以下列表选：GaN/SiC/BCD/AC-DC/DC-DC/PMIC/LDO/MOSFET/Driver/Analog/Signal Chain/功率器件/其他
    "companies": ["芯朋微", "TI"],         // 关联企业列表
    "confidence": 0.85                     // 分析置信度 0-1
}}

注意：impact_score 评分说明：
1 = 几乎无影响
2 = 轻微影响
3 = 中等影响（值得关注）
4 = 重大影响（需要应对）
5 = 决定性影响（战略级）

risk_level 与影响评分对应：
L1轻微 = 影响评分1
L2关注 = 影响评分2
L3预警 = 影响评分3
L4严重 = 影响评分4
L5危机 = 影响评分5
"""

    def _parse_llm_response(self, response_text: str) -> Optional[Dict]:
        """解析 LLM 返回的 JSON"""
        # 尝试直接解析
        text = response_text.strip()
        if text.startswith("```"):
            # 移除 markdown 代码块
            text = re.sub(r'^```(?:json)?\s*', '', text)
            text = re.sub(r'\s*```$', '', text)

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # 尝试提取 JSON 片段
        match = re.search(r'\{[^{}]*\}', text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
        return None

    def _analyze_with_llm(self, title: str, content: str) -> NLPAnalysis:
        """使用 LLM 进行分析"""
        prompt = self._build_prompt(title, content)
        result = NLPAnalysis()

        try:
            messages = [
                {"role": "system", "content": "你是一个专业的半导体行业舆情分析师。严格按要求的JSON格式返回，不要多余文字。"},
                {"role": "user", "content": prompt}
            ]
            response = self.client.chat(messages)
            parsed = self._parse_llm_response(response)

            if parsed:
                result.sentiment = parsed.get("sentiment", "中性")
                result.sentiment_score = float(parsed.get("sentiment_score", 0.5))
                result.impact_dimension = parsed.get("impact_dimension", "中性")
                result.impact_score = int(parsed.get("impact_score", 0))
                result.risk_level = parsed.get("risk_level", "L1轻微")
                result.pest_dimension = parsed.get("pest_dimension", "综合")
                result.pest_analysis = parsed.get("pest_analysis", "")
                result.tech_tags = parsed.get("tech_tags", [])
                result.companies = parsed.get("companies", [])
                result.confidence = float(parsed.get("confidence", 0.5))
            else:
                # LLM 返回格式不对，回退到规则分析
                fallback = self._analyze_with_rules(title, content)
                fallback.confidence = 0.3
                return fallback

        except Exception as e:
            print(f"[NLP] LLM 分析失败: {e}")
            fallback = self._analyze_with_rules(title, content)
            fallback.confidence = 0.2
            return fallback

        return result

    def _analyze_with_rules(self, title: str, content: str) -> NLPAnalysis:
        """基于关键词规则的兜底分析"""
        text = f"{title} {content}".lower()

        result = NLPAnalysis()
        result.tech_tags = []
        result.companies = []

        # 技术标签匹配
        for tag, keywords in TECH_KEYWORDS.items():
            for kw in keywords:
                if kw.lower() in text:
                    if tag not in result.tech_tags:
                        result.tech_tags.append(tag)
                    break

        if not result.tech_tags:
            result.tech_tags.append("其他")

        # 关联企业匹配
        for company, patterns in COMPANY_PATTERNS.items():
            for pat in patterns:
                if pat.lower() in text:
                    if company not in result.companies:
                        result.companies.append(company)
                    break

        # PEST 归类
        pest_scores = {"政治": 0, "经济": 0, "社会": 0, "技术": 0}
        for dim, keywords in PEST_KEYWORDS.items():
            for kw in keywords:
                if kw.lower() in text:
                    pest_scores[dim] += 1
        max_dim = max(pest_scores, key=pest_scores.get)
        if pest_scores[max_dim] > 0:
            result.pest_dimension = max_dim
        else:
            result.pest_dimension = "综合"
        result.pest_analysis = f"规则匹配: {result.pest_dimension}维度"

        # 情感/影响/风险 - 基于负面关键词
        negative_words = ["危机", "事故", "召回", "罚款", "诉讼", "下跌", "亏损",
                          "裁员", "停产", "断供", "制裁", "禁令", "风险", "警告",
                          "故障", "缺陷", "违规", "处罚", "查封"]
        positive_words = ["突破", "创新", "增长", "获奖", "认证", "上市", "融资",
                          "合作", "签约", "投产", "扩产", "提升", "突破", "首发"]

        neg_count = sum(1 for w in negative_words if w in text)
        pos_count = sum(1 for w in positive_words if w in text)

        if neg_count > pos_count:
            result.sentiment = "负面"
            result.sentiment_score = max(0.0, 0.5 - neg_count * 0.1)
            result.impact_dimension = "负面影响"
            result.impact_score = min(5, max(1, neg_count))
        elif pos_count > neg_count:
            result.sentiment = "正面"
            result.sentiment_score = min(1.0, 0.5 + pos_count * 0.1)
            result.impact_dimension = "正面影响"
            result.impact_score = min(5, max(1, pos_count))
        else:
            result.sentiment = "中性"
            result.sentiment_score = 0.5
            result.impact_dimension = "中性"
            result.impact_score = 1

        # 风险等级映射
        risk_map = {1: "L1轻微", 2: "L2关注", 3: "L3预警", 4: "L4严重", 5: "L5危机"}
        result.risk_level = risk_map.get(result.impact_score, "L1轻微")
        result.confidence = 0.5

        return result

    def analyze_item(self, item: Union[NewsItem, RSSItem], content: str = "") -> NLPAnalysis:
        """
        分析 NewsItem 或 RSSItem

        Args:
            item: 新闻条目对象
            content: 可选的正文/摘要覆盖

        Returns:
            NLPAnalysis 分析结果
        """
        title = item.title
        if hasattr(item, 'summary') and item.summary:
            content = content or item.summary
        elif hasattr(item, 'content') and item.content:
            content = content or item.content
        return self.analyze(title, content)

    def analyze_batch(self, items: List[Union[NewsItem, RSSItem]],
                      batch_size: int = 10) -> List[NLPAnalysis]:
        """
        批量分析

        Args:
            items: 新闻条目列表
            batch_size: 每批大小

        Returns:
            分析结果列表
        """
        results = []
        for i, item in enumerate(items):
            if i > 0 and i % batch_size == 0:
                print(f"[NLP] 已分析 {i}/{len(items)} 条")
            results.append(self.analyze_item(item))
        return results

    def analyze_and_enrich(self, item: Union[NewsItem, RSSItem],
                          content: str = "") -> Dict[str, Any]:
        """
        分析并返回可直接写入多维表格的 enriched 字段

        Args:
            item: 新闻条目
            content: 正文/摘要

        Returns:
            字段字典，可直接 merge 到 bitable 记录
        """
        r = self.analyze_item(item, content)
        fields = {}
        if r.sentiment:
            fields["情感倾向"] = r.sentiment
        if r.sentiment_score > 0:
            fields["情感分值"] = r.sentiment_score
        if r.impact_dimension:
            fields["影响维度"] = r.impact_dimension
        if r.impact_score > 0:
            fields["影响评分"] = r.impact_score
        if r.risk_level:
            fields["风险等级"] = r.risk_level
        if r.pest_dimension:
            fields["PEST维度"] = r.pest_dimension
        if r.pest_analysis:
            fields["PEST分析"] = r.pest_analysis
        if r.tech_tags:
            fields["技术标签"] = r.tech_tags
        if r.companies:
            fields["关联企业"] = list(set(fields.get("关联企业", []) + r.companies))
        return fields
