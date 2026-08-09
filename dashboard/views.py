"""dashboard views: 运行日志数据看板。

统计全部来自既有模型（kb.models + auth user），无需新增 model/迁移。
- 顶部 4 张统计卡（用户/文档/向量块/会话）+ 今日活跃。
- 14 天问答趋势（Message 按天聚合，user/ai 两条线）。
- 文档处理状态分布（Document 按 status 聚合）。
- 手册库一览（每库的文档/向量数 + 状态拆分）。
- 最近会话活动。

注意：LLM token 用量仅在每次请求内存累计，未持久化，故看板不含 token 统计。
"""
from __future__ import annotations

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db.models import Count, Q, Sum
from django.db.models.functions import TruncDate
from django.shortcuts import render
from django.utils import timezone

from kb.models import Conversation, Document, KnowledgeBase, Message

User = get_user_model()

# 趋势图天数
TREND_DAYS = 14
# 文档状态 → 展示顺序 + Inspection Tag 配色（与 fos.css 徽标一致）
DOC_STATUS_META = [
    ("completed", "已完成", "#2E7D43"),   # ok green
    ("ocr", "OCR 中", "#E0A100"),         # warn amber
    ("indexing", "向量化中", "#C2362C"),  # danger red
    ("pending", "待处理", "#807A6C"),     # mute grey
    ("failed", "失败", "#9E2A22"),        # dark danger
]


def _today():
    """当前时区当天的 0 点（用于“今日”统计）。"""
    return timezone.localtime(timezone.now()).replace(hour=0, minute=0, second=0, microsecond=0)


def _trend_series():
    """构建近 N 天的问答趋势：user 问题数 + ai 回复数。

    返回 dict，含日期标签、两条序列、SVG 坐标点与 y 轴最大值（已预计算，
    模板只负责输出，避免在模板里做数学运算）。
    """
    start = _today() - timedelta(days=TREND_DAYS - 1)
    qs = (
        Message.objects
        .filter(created_at__gte=start)
        .annotate(day=TruncDate("created_at"))
        .values("day", "role")
        .annotate(c=Count("id"))
    )
    # day(.date) -> {"user": n, "ai": m}
    buckets: dict = {}
    for row in qs:
        d = row["day"]
        buckets.setdefault(d, {"user": 0, "ai": 0})[row["role"]] = row["c"]

    days = [start.date() + timedelta(days=i) for i in range(TREND_DAYS)]
    user_vals = [buckets.get(d, {"user": 0})["user"] for d in days]
    ai_vals = [buckets.get(d, {"ai": 0})["ai"] for d in days]
    max_val = max([*user_vals, *ai_vals, 1])

    # SVG 画布尺寸（viewBox，自适应宽度）
    W, H = 560, 180
    pad_l, pad_r, pad_t, pad_b = 28, 12, 14, 26
    plot_w = W - pad_l - pad_r
    plot_h = H - pad_t - pad_b

    def to_points(vals):
        n = len(vals)
        if n == 1:
            x = pad_l + plot_w / 2
            y = pad_t + plot_h - (vals[0] / max_val) * plot_h
            return [(x, y)]
        pts = []
        for i, v in enumerate(vals):
            x = pad_l + (i / (n - 1)) * plot_w
            y = pad_t + plot_h - (v / max_val) * plot_h
            pts.append((x, y))
        return pts

    user_pts = to_points(user_vals)
    ai_pts = to_points(ai_vals)

    def to_path(pts):
        return " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)

    # x 轴标签：每天一个会太密，隔 2 天标一次
    x_labels = [
        {
            "x": pad_l + (i / max(len(days) - 1, 1)) * plot_w,
            "text": d.strftime("%m-%d"),
            "show": i % 2 == 0,
        }
        for i, d in enumerate(days)
    ]
    # y 轴：0 / max 两档刻度
    y_ticks = [
        {"y": pad_t + plot_h, "label": "0"},
        {"y": pad_t, "label": str(max_val)},
    ]

    return {
        "labels": [d.strftime("%m-%d") for d in days],
        "user_vals": user_vals,
        "ai_vals": ai_vals,
        "user_total": sum(user_vals),
        "ai_total": sum(ai_vals),
        "max_val": max_val,
        "svg": {
            "w": W, "h": H, "pad_l": pad_l, "pad_r": pad_r, "pad_t": pad_t, "pad_b": pad_b,
            "plot_w": plot_w, "plot_h": plot_h,
            "user_path": to_path(user_pts),
            "ai_path": to_path(ai_pts),
            "user_pts": user_pts,
            "ai_pts": ai_pts,
            "x_labels": x_labels,
            "y_ticks": y_ticks,
            "base_y": pad_t + plot_h,
        },
    }


def _doc_status_bars():
    """文档按 status 聚合 → 水平条形（每条: label/颜色/数量/比例）。"""
    counts = (
        Document.objects
        .values("status")
        .annotate(c=Count("id"))
    )
    cmap = {row["status"]: row["c"] for row in counts}
    total = sum(cmap.values())
    bars = []
    for code, label, color in DOC_STATUS_META:
        n = cmap.get(code, 0)
        bars.append({
            "code": code,
            "label": label,
            "color": color,
            "count": n,
            "pct": (n / total * 100) if total else 0,
        })
    return {"bars": bars, "total": total}


def _kb_table():
    """手册库一览：每库文档/向量数 + 状态拆分。"""
    kbs = list(
        KnowledgeBase.objects
        .annotate(
            doc_total=Count("documents"),
            doc_completed=Count("documents", filter=Q(documents__status="completed")),
            doc_failed=Count("documents", filter=Q(documents__status="failed")),
            doc_in_progress=Count(
                "documents",
                filter=Q(documents__status__in=["pending", "ocr", "indexing"]),
            ),
        )
        .order_by("-created_at")
    )
    return kbs


def _recent_activity(limit=8):
    """最近会话活动。"""
    return list(
        Conversation.objects
        .select_related("user", "kb")
        .order_by("-updated_at")[:limit]
    )


def index(request):
    """运行日志看板主页（真实统计）。"""
    today = _today()
    total_chunks = (
        KnowledgeBase.objects.aggregate(s=Sum("chunk_count"))["s"] or 0
    )
    # 今日活跃：今天有新消息或会话更新的不同用户数
    active_today = (
        Conversation.objects
        .filter(updated_at__gte=today)
        .values("user_id")
        .distinct()
        .count()
    )

    ctx = {
        "generated_at": timezone.localtime(timezone.now()),
        "stat_users": User.objects.count(),
        "stat_docs": Document.objects.count(),
        "stat_chunks": total_chunks,
        "stat_convs": Conversation.objects.count(),
        "stat_messages": Message.objects.count(),
        "active_today": active_today,
        "trend": _trend_series(),
        "doc_bars": _doc_status_bars(),
        "kbs": _kb_table(),
        "recent": _recent_activity(),
    }
    return render(request, "dashboard/index.html", ctx)
