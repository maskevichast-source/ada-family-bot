"""Комплексная визуализация финансов — графики для Power BI и Telegram."""

import io
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from datetime import datetime, timedelta
from collections import defaultdict

from services.sheets import get_transactions_for_period, get_category_limits
from services.categories import TYPE_EXPENSE, TYPE_INCOME, SUBCATEGORIES_MAP


def _parse_date(date_str: str) -> datetime:
    """Парсинг даты из строки таблицы."""
    try:
        return datetime.strptime(str(date_str), "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        try:
            return datetime.strptime(str(date_str)[:10], "%Y-%m-%d")
        except (ValueError, TypeError):
            return datetime.now()


def _group_by_day(transactions: list) -> dict:
    """Группировка расходов по дням."""
    daily = defaultdict(float)
    for t in transactions:
        if str(t.get("type")) != TYPE_EXPENSE:
            continue
        try:
            amt = float(str(t.get("amt", 0)).replace(",", "."))
            d = _parse_date(t.get("date"))
            daily[d.strftime("%Y-%m-%d")] += amt
        except (ValueError, TypeError):
            pass
    return dict(sorted(daily.items()))


def _group_by_category(transactions: list) -> dict:
    """Группировка расходов по категориям."""
    cats = defaultdict(float)
    for t in transactions:
        if str(t.get("type")) != TYPE_EXPENSE:
            continue
        try:
            amt = float(str(t.get("amt", 0)).replace(",", "."))
            cat = str(t.get("cat") or "Прочее")
            cats[cat] += amt
        except (ValueError, TypeError):
            pass
    return dict(sorted(cats.items(), key=lambda x: -x[1]))


def _group_by_subcategory(transactions: list) -> dict:
    """Группировка расходов по подкатегориям."""
    subs = defaultdict(float)
    for t in transactions:
        if str(t.get("type")) != TYPE_EXPENSE:
            continue
        try:
            amt = float(str(t.get("amt", 0)).replace(",", "."))
            sub = str(t.get("subcat") or "Прочее")
            subs[sub] += amt
        except (ValueError, TypeError):
            pass
    return dict(sorted(subs.items(), key=lambda x: -x[1]))


def _group_by_necessity(transactions: list) -> dict:
    """Need vs Want."""
    nec = defaultdict(float)
    for t in transactions:
        if str(t.get("type")) != TYPE_EXPENSE:
            continue
        try:
            amt = float(str(t.get("amt", 0)).replace(",", "."))
            n = str(t.get("nec") or "Need")
            nec[n] += amt
        except (ValueError, TypeError):
            pass
    return nec


def _group_by_user(transactions: list) -> dict:
    """Расходы по пользователям."""
    users = defaultdict(float)
    for t in transactions:
        if str(t.get("type")) != TYPE_EXPENSE:
            continue
        try:
            amt = float(str(t.get("amt", 0)).replace(",", "."))
            u = str(t.get("user") or "Неизвестно")
            users[u] += amt
        except (ValueError, TypeError):
            pass
    return users


def _income_vs_expense(transactions: list) -> tuple[float, float]:
    """Сумма доходов и расходов."""
    income = 0.0
    expense = 0.0
    for t in transactions:
        try:
            amt = float(str(t.get("amt", 0)).replace(",", "."))
            if str(t.get("type")) == TYPE_INCOME:
                income += amt
            else:
                expense += amt
        except (ValueError, TypeError):
            pass
    return income, expense


def _get_month_range(year: int, month: int) -> tuple[str, str]:
    """Вернуть начало и конец месяца в формате YYYY-MM-DD."""
    start = datetime(year, month, 1)
    if month == 12:
        end = datetime(year + 1, 1, 1)
    else:
        end = datetime(year, month + 1, 1)
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


# ═══════════════════════════════════════════════════════════════════════════════
# ГЛАВНЫЙ ГРАФИК — комплексный дашборд
# ═══════════════════════════════════════════════════════════════════════════════

def generate_expense_chart(year: int = None, month: int = None) -> bytes | None:
    """Создать комплексный дашборд расходов (6 графиков на одном изображении).

    Размер: 1600x1200 px — оптимально для Telegram и Power BI.
    """
    now = datetime.now()
    year = year or now.year
    month = month or now.month

    start_str, end_str = _get_month_range(year, month)
    transactions = get_transactions_for_period(start_str, end_str)

    if not transactions:
        return None

    # Фильтруем только расходы для большинства графиков
    expenses = [t for t in transactions if str(t.get("type")) != TYPE_INCOME]

    if not expenses:
        return None

    # Данные
    by_cat = _group_by_category(expenses)
    by_sub = _group_by_subcategory(expenses)
    by_day = _group_by_day(expenses)
    by_nec = _group_by_necessity(expenses)
    by_user = _group_by_user(expenses)
    income_total, expense_total = _income_vs_expense(transactions)
    limits = get_category_limits()

    # Цветовая палитра
    COLORS = {
        'primary': ['#FF6B6B', '#4ECDC4', '#45B7D1', '#96CEB4', '#FFEAA7',
                    '#DDA0DD', '#98D8C8', '#F7DC6F', '#BB8FCE', '#85C1E9',
                    '#FF9999', '#66B2FF', '#99FF99', '#FFCC99', '#FF99CC'],
        'need': '#2ECC71',
        'want': '#E74C3C',
        'income': '#27AE60',
        'expense': '#E74C3C',
        'limit': '#F39C12',
        'bg': '#1a1a2e',
        'text': '#ecf0f1',
        'grid': '#2d2d44',
    }

    # Создаём фигуру 2x3
    fig = plt.figure(figsize=(16, 12), facecolor=COLORS['bg'])
    fig.suptitle(
        f'📊 Семейный бюджет — {datetime(year, month, 1).strftime("%B %Y").capitalize()}',
        fontsize=18, fontweight='bold', color=COLORS['text'], y=0.98
    )

    # ── 1. Круговая диаграмма по категориям (топ-8) ──
    ax1 = fig.add_subplot(2, 3, 1)
    ax1.set_facecolor(COLORS['bg'])

    top_cats = list(by_cat.items())[:8]
    other = sum(v for _, v in list(by_cat.items())[8:])
    if other > 0:
        top_cats.append(("Прочее", other))

    labels1 = [cat[:20] for cat, _ in top_cats]
    values1 = [v for _, v in top_cats]
    colors1 = COLORS['primary'][:len(labels1)]

    wedges, texts, autotexts = ax1.pie(
        values1, labels=labels1, autopct=lambda pct: f'{pct:.1f}%' if pct > 3 else '',
        colors=colors1, startangle=90, textprops={'color': COLORS['text'], 'fontsize': 8}
    )
    ax1.set_title('Расходы по категориям', color=COLORS['text'], fontsize=12, pad=10)

    # ── 2. Столбчатая диаграмма по дням ──
    ax2 = fig.add_subplot(2, 3, 2)
    ax2.set_facecolor(COLORS['bg'])

    if by_day:
        days = list(by_day.keys())
        day_vals = list(by_day.values())
        # Показываем только дни с тратами, форматируем как "01", "02" и т.д.
        day_labels = [d[-2:] for d in days]

        bars = ax2.bar(day_labels, day_vals, color=COLORS['primary'][0], alpha=0.8, width=0.7)
        ax2.set_xlabel('День месяца', color=COLORS['text'], fontsize=9)
        ax2.set_ylabel('Сумма, тг', color=COLORS['text'], fontsize=9)
        ax2.set_title('Расходы по дням', color=COLORS['text'], fontsize=12, pad=10)
        ax2.tick_params(colors=COLORS['text'], labelsize=7)
        ax2.grid(axis='y', alpha=0.2, color=COLORS['grid'])

        # Подписи на столбцах
        for bar, val in zip(bars, day_vals):
            if val > max(day_vals) * 0.15:
                ax2.text(bar.get_x() + bar.get_width()/2, bar.get_height() + max(day_vals)*0.02,
                        f'{val:,.0f}', ha='center', va='bottom', color=COLORS['text'], fontsize=6)

    # ── 3. Need vs Want ──
    ax3 = fig.add_subplot(2, 3, 3)
    ax3.set_facecolor(COLORS['bg'])

    nec_labels = list(by_nec.keys())
    nec_values = list(by_nec.values())
    nec_colors = [COLORS['need'] if 'eed' in l else COLORS['want'] for l in nec_labels]

    bars3 = ax3.bar(nec_labels, nec_values, color=nec_colors, alpha=0.85, width=0.5)
    ax3.set_ylabel('Сумма, тг', color=COLORS['text'], fontsize=9)
    ax3.set_title('Need vs Want', color=COLORS['text'], fontsize=12, pad=10)
    ax3.tick_params(colors=COLORS['text'], labelsize=9)
    ax3.grid(axis='y', alpha=0.2, color=COLORS['grid'])

    for bar, val in zip(bars3, nec_values):
        ax3.text(bar.get_x() + bar.get_width()/2, bar.get_height() + max(nec_values)*0.02,
                f'{val:,.0f} тг', ha='center', va='bottom', color=COLORS['text'], fontsize=9, fontweight='bold')

    # ── 4. Сравнение с лимитами (горизонтальные столбцы) ──
    ax4 = fig.add_subplot(2, 3, 4)
    ax4.set_facecolor(COLORS['bg'])

    limit_data = []
    for cat, spent in by_cat.items():
        limit = limits.get(cat, 0)
        if limit > 0:
            pct = min(spent / limit * 100, 150)
            limit_data.append((cat[:18], spent, limit, pct))

    limit_data = sorted(limit_data, key=lambda x: -x[3])[:10]

    if limit_data:
        cats_l = [d[0] for d in limit_data]
        pcts = [d[3] for d in limit_data]
        bar_colors = [COLORS['want'] if p > 100 else COLORS['need'] if p > 75 else COLORS['primary'][2] for p in pcts]

        bars4 = ax4.barh(cats_l, pcts, color=bar_colors, alpha=0.85, height=0.6)
        ax4.axvline(x=100, color=COLORS['limit'], linestyle='--', linewidth=2, label='Лимит 100%')
        ax4.set_xlabel('% от лимита', color=COLORS['text'], fontsize=9)
        ax4.set_title('Выполнение лимитов', color=COLORS['text'], fontsize=12, pad=10)
        ax4.tick_params(colors=COLORS['text'], labelsize=7)
        ax4.grid(axis='x', alpha=0.2, color=COLORS['grid'])
        ax4.invert_yaxis()
        ax4.legend(loc='lower right', fontsize=8, facecolor=COLORS['bg'], edgecolor=COLORS['grid'],
                   labelcolor=COLORS['text'])

        for bar, pct in zip(bars4, pcts):
            ax4.text(bar.get_width() + 2, bar.get_y() + bar.get_height()/2,
                    f'{pct:.0f}%', ha='left', va='center', color=COLORS['text'], fontsize=7)

    # ── 5. Доходы vs Расходы + баланс ──
    ax5 = fig.add_subplot(2, 3, 5)
    ax5.set_facecolor(COLORS['bg'])

    categories5 = ['Доходы', 'Расходы']
    values5 = [income_total, expense_total]
    colors5 = [COLORS['income'], COLORS['expense']]

    bars5 = ax5.bar(categories5, values5, color=colors5, alpha=0.85, width=0.5)
    ax5.set_ylabel('Сумма, тг', color=COLORS['text'], fontsize=9)
    ax5.set_title('Доходы vs Расходы', color=COLORS['text'], fontsize=12, pad=10)
    ax5.tick_params(colors=COLORS['text'], labelsize=9)
    ax5.grid(axis='y', alpha=0.2, color=COLORS['grid'])

    for bar, val in zip(bars5, values5):
        ax5.text(bar.get_x() + bar.get_width()/2, bar.get_height() + max(values5)*0.02,
                f'{val:,.0f} тг', ha='center', va='bottom', color=COLORS['text'], fontsize=9, fontweight='bold')

    # Баланс текстом
    balance = income_total - expense_total
    balance_color = COLORS['income'] if balance >= 0 else COLORS['expense']
    ax5.text(0.5, -0.15, f'Баланс: {balance:,.0f} тг',
            transform=ax5.transAxes, ha='center', va='top',
            color=balance_color, fontsize=11, fontweight='bold')

    # ── 6. Расходы по пользователям ──
    ax6 = fig.add_subplot(2, 3, 6)
    ax6.set_facecolor(COLORS['bg'])

    if by_user:
        user_names = list(by_user.keys())
        user_vals = list(by_user.values())
        colors6 = COLORS['primary'][:len(user_names)]

        wedges6, texts6, autotexts6 = ax6.pie(
            user_vals, labels=user_names, autopct='%1.1f%%',
            colors=colors6, startangle=90, textprops={'color': COLORS['text'], 'fontsize': 9}
        )
        ax6.set_title('Кто сколько потратил', color=COLORS['text'], fontsize=12, pad=10)

    plt.tight_layout(rect=[0, 0, 1, 0.95])

    buf = io.BytesIO()
    plt.savefig(buf, format='png', dpi=150, bbox_inches='tight', facecolor=COLORS['bg'])
    buf.seek(0)
    plt.close(fig)

    return buf.getvalue()


# ═══════════════════════════════════════════════════════════════════════════════
# ДОПОЛНИТЕЛЬНЫЕ ГРАФИКИ
# ═══════════════════════════════════════════════════════════════════════════════

def generate_trend_chart(months_back: int = 3) -> bytes | None:
    """График тренда расходов по месяцам."""
    now = datetime.now()
    monthly_data = defaultdict(float)

    for i in range(months_back, -1, -1):
        dt = now - timedelta(days=i*30)
        start, end = _get_month_range(dt.year, dt.month)
        txs = get_transactions_for_period(start, end)
        expense = sum(float(str(t.get("amt", 0)).replace(",", "."))
                      for t in txs if str(t.get("type")) != TYPE_INCOME)
        monthly_data[f"{dt.year}-{dt.month:02d}"] = expense

    if not monthly_data:
        return None

    fig, ax = plt.subplots(figsize=(10, 5), facecolor='#1a1a2e')
    ax.set_facecolor('#1a1a2e')

    months = list(monthly_data.keys())
    values = list(monthly_data.values())

    ax.plot(months, values, marker='o', linewidth=2.5, markersize=8,
            color='#4ECDC4', markerfacecolor='#FF6B6B')
    ax.fill_between(months, values, alpha=0.2, color='#4ECDC4')

    ax.set_ylabel('Сумма, тг', color='#ecf0f1', fontsize=10)
    ax.set_title('Динамика расходов по месяцам', color='#ecf0f1', fontsize=14, fontweight='bold')
    ax.tick_params(colors='#ecf0f1', labelsize=8)
    ax.grid(alpha=0.2, color='#2d2d44')

    for x, y in zip(months, values):
        ax.text(x, y + max(values)*0.03, f'{y:,.0f}', ha='center', va='bottom',
                color='#ecf0f1', fontsize=8)

    plt.tight_layout()
    buf = io.BytesIO()
    plt.savefig(buf, format='png', dpi=150, bbox_inches='tight', facecolor='#1a1a2e')
    buf.seek(0)
    plt.close(fig)
    return buf.getvalue()