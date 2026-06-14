# coding=utf-8
"""
飞书多维表格输出模块

将 TrendRadar 的爬取结果（热榜 + RSS）写入飞书多维表格「情报仓库」。
使用飞书 Open API 直接操作，适用于 GitHub Actions 环境。

使用方式:
    from trendradar.output.feishu_bitable import FeishuBitableWriter

    writer = FeishuBitableWriter(app_id, app_secret)
    writer.write_news_data(news_data)     # 写热榜数据
    writer.write_rss_data(rss_data)       # 写 RSS 数据

环境变量配置:
    FEISHU_APP_ID:     飞书自建应用 App ID
    FEISHU_APP_SECRET: 飞书自建应用 App Secret
    BITABLE_APP_TOKEN: 多维表格 App Token（情报仓库）
    BITABLE_TABLE_ID:  数据表 ID（情报原文库）
"""

import os
import json
import time
import datetime
from typing import Any, Dict, List, Optional, Union

import requests

from trendradar.storage.base import NewsItem, RSSItem, NewsData, RSSData


# === 飞书 API 常量 ===
FEISHU_BASE = "https://open.feishu.cn/open-apis"
BITABLE_APP_TOKEN = os.environ.get("BITABLE_APP_TOKEN", "FB8gbsUe3afZGIsSMsYcXHQGnqd")
BITABLE_TABLE_ID = os.environ.get("BITABLE_TABLE_ID", "tblmNM2rZ3ISNHnw")

# 字段 ID 映射（字段名 → field_id，建表后固定不变）
FIELD_IDS = {
    "标题": "fldzmzPMco",
    "原文链接": "fldR9VErB2",
    "摘要": "fld05AKIqe",
    "来源平台": "fldBPCjXqu",
    "情报分类": "fldQicwfDY",
    "关联企业": "fldlOUpXSR",
    "波特五力角色": "fldxbo7jRf",
    "技术标签": "fldIap6WFK",
    "情感倾向": "fldtEVStK8",
    "情感分值": "fldRumQ1QO",
    "影响维度": "fldtVpr6cL",
    "影响评分": "fldlto78hs",
    "风险等级": "fldDa59zxi",
    "PEST维度": "fldduJGhV1",
    "PEST分析": "fldGEgUyoX",
    "查看次数": "fldi03leXm",
    "点赞次数": "fld6Y14ktK",
    "收藏次数": "fld7XmoDMv",
    "转发次数": "fldSnoZjge",
    "热度值": "fldRA4M7b4",
    "原文时间": "fldBUXWhKT",
    "采集时间": "fldHZ3eUx7",
    "关联项目": "fldcJ3veB3",
    "状态": "fldGv67CrM",
    "备注": "fldJ5rA1pV",
}

# 选项名映射（写入记录时用选项名即可，飞书自动匹配）
PLATFORM_MAP = {
    "头条": "头条",
    "toutiao": "头条",
    "36氪": "36氪",
    "36kr": "36氪",
    "虎嗅": "虎嗅",
    "huxiu": "虎嗅",
    "微博": "微博",
    "weibo": "微博",
    "知乎": "知乎",
    "zhihu": "知乎",
    "雪球": "雪球",
    "xueqiu": "雪球",
    "集微网": "集微网",
    "jiwei": "集微网",
    "半导体行业观察": "半导体行业观察",
    "CSDN": "CSDN",
    "csdn": "CSDN",
    "GitHub": "GitHub",
    "github": "GitHub",
}

CATEGORY_KEYWORDS = {
    "技术": "技术突破",
    "芯片": "行业动态",
    "半导体": "行业动态",
    "政策": "政策监管",
    "法规": "政策监管",
    "市场": "市场数据",
    "数据": "市场数据",
    "融资": "投融资",
    "投资": "投融资",
    "收购": "投融资",
    "财报": "企业动态",
    "业绩": "企业动态",
    "竞争": "竞争分析",
    "对标": "竞争分析",
    "危机": "舆情危机",
    "事故": "舆情危机",
    "召回": "舆情危机",
}

COMPANY_KEYWORDS = {
    "芯朋微": "芯朋微",
    "圣邦微": "圣邦微",
    "思瑞浦": "思瑞浦",
    "纳芯微": "纳芯微",
    "艾为": "艾为电子",
    "力芯微": "力芯微",
    "希荻微": "希荻微",
    "晶丰明源": "晶丰明源",
    "富满": "富满电子",
    "明微": "明微电子",
    "TI ": "TI",
    "德州仪器": "TI",
    "ADI": "ADI",
    "亚德诺": "ADI",
    "英飞凌": "英飞凌",
    "infineon": "英飞凌",
    "安森美": "安森美",
    "onsemi": "安森美",
    "意法半导体": "意法半导体",
    "STMicro": "意法半导体",
    "ST ": "意法半导体",
}


class FeishuAuth:
    """飞书 API 认证（tenant_access_token）"""

    def __init__(self, app_id: str, app_secret: str):
        self.app_id = app_id
        self.app_secret = app_secret
        self._token = ""
        self._expires_at = 0

    def get_token(self) -> str:
        """获取有效的 tenant_access_token（自动刷新）"""
        now = time.time()
        if self._token and now < self._expires_at - 60:
            return self._token

        resp = requests.post(
            f"{FEISHU_BASE}/auth/v3/tenant_access_token/internal",
            json={"app_id": self.app_id, "app_secret": self.app_secret},
            timeout=10,
        )
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"获取 tenant_access_token 失败: {data}")

        self._token = data["tenant_access_token"]
        self._expires_at = now + data.get("expire", 7200)
        return self._token


class FeishuBitableWriter:
    """飞书多维表格写入器"""

    def __init__(
        self,
        app_id: str = "",
        app_secret: str = "",
        app_token: str = "",
        table_id: str = "",
    ):
        self.app_id = app_id or os.environ.get("FEISHU_APP_ID", "")
        self.app_secret = app_secret or os.environ.get("FEISHU_APP_SECRET", "")
        self.app_token = app_token or BITABLE_APP_TOKEN
        self.table_id = table_id or BITABLE_TABLE_ID
        self.auth = FeishuAuth(self.app_id, self.app_secret)

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.auth.get_token()}",
            "Content-Type": "application/json",
        }

    def _guess_platform(self, source_id: str, source_name: str) -> str:
        """根据 source_id 或 source_name 推断来源平台"""
        for key, name in PLATFORM_MAP.items():
            if key.lower() in source_id.lower() or key.lower() in source_name.lower():
                return name
        return source_name if source_name else "其他"

    def _guess_category(self, title: str, summary: str = "") -> str:
        """根据标题和摘要推断情报分类"""
        text = f"{title} {summary}".lower()
        for keyword, category in CATEGORY_KEYWORDS.items():
            if keyword.lower() in text:
                return category
        return "行业动态"

    def _guess_companies(self, title: str, summary: str = "") -> List[str]:
        """根据标题和摘要提取关联企业"""
        text = f"{title} {summary}"
        companies = []
        for keyword, name in COMPANY_KEYWORDS.items():
            if keyword.lower() in text.lower():
                if name not in companies:
                    companies.append(name)
        return companies

    def _to_timestamp_ms(self, time_str: str) -> Optional[int]:
        """将时间字符串转为毫秒时间戳"""
        if not time_str:
            return None
        try:
            # 尝试 ISO 格式
            dt = datetime.datetime.fromisoformat(time_str)
            return int(dt.timestamp() * 1000)
        except (ValueError, TypeError):
            pass
        try:
            # 尝试 HH:MM 格式（热榜数据），使用今天日期
            today = datetime.date.today().isoformat()
            dt = datetime.datetime.strptime(f"{today} {time_str}", "%Y-%m-%d %H:%M")
            return int(dt.timestamp() * 1000)
        except (ValueError, TypeError):
            pass
        return None

    def _build_record(self, title: str, url: str, summary: str,
                     platform: str, category: str, companies: List[str],
                     tags: List[str], publish_time: Optional[int],
                     crawl_time: Optional[int], source_type: str,
                     score: float = 0.0) -> Dict[str, Any]:
        """构建一条多维表格记录"""
        fields = {}
        # 基本信息
        if title:
            fields["标题"] = title
        if url:
            fields["原文链接"] = {"link": url, "text": title[:50] if title else url}
        if summary:
            fields["摘要"] = summary[:100000]  # 飞书单单元格上限10万字符
        fields["来源平台"] = platform
        fields["情报分类"] = category
        if companies:
            fields["关联企业"] = companies
        # 时间
        if publish_time:
            fields["原文时间"] = publish_time
        now_ms = int(time.time() * 1000)
        fields["采集时间"] = crawl_time or now_ms
        # 状态
        fields["状态"] = "待处理"
        # 来源类型写入备注
        fields["备注"] = f"来源类型: {source_type}"
        # 情感/影响评分基础值（后续由 NLP 模块精调）
        return fields

    def _write_batch(self, records: List[Dict[str, Any]], batch_label: str) -> int:
        """批量写入记录，返回成功写入条数"""
        if not records:
            return 0

        # 分批，每批最多 500 条
        batch_size = 500
        total_written = 0
        total = len(records)

        for i in range(0, total, batch_size):
            batch = records[i:i + batch_size]
            items = [{"fields": r} for r in batch]
            payload = {"records": items}

            try:
                resp = requests.post(
                    f"{FEISHU_BASE}/bitable/v1/apps/{self.app_token}/tables/{self.table_id}/records/batch_create",
                    headers=self._headers(),
                    json=payload,
                    timeout=30,
                )
                data = resp.json()
                if data.get("code") == 0:
                    written = len(batch)
                    total_written += written
                    print(f"[Bitable] 已写入 {batch_label}: {total_written}/{total}")
                else:
                    print(f"[Bitable] 写入失败 (batch {i//batch_size+1}): {data}")
                    # 如果是限频错误，等待后重试
                    if data.get("code") == 99991400:
                        time.sleep(3)
                    elif data.get("code") == 1254291:
                        time.sleep(2)
            except Exception as e:
                print(f"[Bitable] 写入异常 (batch {i//batch_size+1}): {e}")
                time.sleep(1)

        return total_written

    def write_news_data(self, news_data: NewsData) -> int:
        """
        写入热榜数据到多维表格

        Args:
            news_data: 热榜数据集合

        Returns:
            写入记录数
        """
        if not news_data or not news_data.items:
            print("[Bitable] 热榜数据为空，跳过")
            return 0

        records = []
        crawl_date = news_data.date
        crawl_time_str = news_data.crawl_time

        # 爬取时间（毫秒时间戳）
        crawl_ms = self._to_timestamp_ms(crawl_time_str)
        if crawl_ms is None and crawl_date:
            try:
                dt = datetime.datetime.strptime(f"{crawl_date} 12:00", "%Y-%m-%d %H:%M")
                crawl_ms = int(dt.timestamp() * 1000)
            except ValueError:
                crawl_ms = int(time.time() * 1000)

        for source_id, news_list in news_data.items.items():
            source_name = news_data.id_to_name.get(source_id, source_id)
            platform = self._guess_platform(source_id, source_name)

            for item in news_list:
                title = item.title
                url = item.url or item.mobile_url or ""
                summary = ""
                companies = self._guess_companies(title)
                category = self._guess_category(title)
                tags = []

                # 热度值 = 根据排名估算
                rank = item.rank if item.rank else 99
                heat_score = max(0.0, round((100 - rank) / 10, 1))

                fields = self._build_record(
                    title=title,
                    url=url,
                    summary=summary,
                    platform=platform,
                    category=category,
                    companies=companies,
                    tags=tags,
                    publish_time=None,  # 热榜数据无原文时间
                    crawl_time=crawl_ms,
                    source_type="hotlist",
                    score=heat_score,
                )
                fields["热度值"] = heat_score
                records.append(fields)

        return self._write_batch(records, "热榜数据")

    def write_rss_data(self, rss_data: RSSData) -> int:
        """
        写入 RSS 数据到多维表格

        Args:
            rss_data: RSS 数据集合

        Returns:
            写入记录数
        """
        if not rss_data or not rss_data.items:
            print("[Bitable] RSS 数据为空，跳过")
            return 0

        records = []
        crawl_date = rss_data.date
        crawl_time_str = rss_data.crawl_time

        # 爬取时间
        crawl_ms = self._to_timestamp_ms(crawl_time_str)
        if crawl_ms is None and crawl_date:
            try:
                dt = datetime.datetime.strptime(f"{crawl_date} 12:00", "%Y-%m-%d %H:%M")
                crawl_ms = int(dt.timestamp() * 1000)
            except ValueError:
                crawl_ms = int(time.time() * 1000)

        for feed_id, rss_list in rss_data.items.items():
            feed_name = rss_data.id_to_name.get(feed_id, feed_id)
            platform = self._guess_platform(feed_id, feed_name)

            for item in rss_list:
                title = item.title
                url = item.url or ""
                summary = item.summary or ""
                companies = self._guess_companies(title, summary)
                category = self._guess_category(title, summary)

                # 原文发布时间
                publish_ms = self._to_timestamp_ms(item.published_at)

                fields = self._build_record(
                    title=title,
                    url=url,
                    summary=summary[:500],  # 摘要截断
                    platform=platform,
                    category=category,
                    companies=companies,
                    tags=[],
                    publish_time=publish_ms or crawl_ms,
                    crawl_time=crawl_ms,
                    source_type="rss",
                )
                records.append(fields)

        return self._write_batch(records, "RSS数据")

    def write_all(self, news_data: Optional[NewsData] = None,
                  rss_data: Optional[RSSData] = None) -> int:
        """
        同时写入热榜和 RSS 数据

        Args:
            news_data: 热榜数据
            rss_data: RSS 数据

        Returns:
            总写入记录数
        """
        total = 0
        if news_data:
            total += self.write_news_data(news_data)
        if rss_data:
            total += self.write_rss_data(rss_data)
        return total
