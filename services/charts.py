"""Построение графиков расходов и доходов."""

import datetime
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from services.timezone import ASTANA_TZ
from services.categories import TYPE_EXPENSE, TYPE_INCOME


CHART_FILE_EXPENSE = Path(__file__).resolve().parent.parent / "spending_chart.png"
CHART_FILE_INCOME = Path(__file__).resolve().parent.parent / "income_chart.png"


def _amount(transaction: dict) -> float:
    raw_amount = transaction.get("amt", transaction.get("amount", 0))
    try:
        return float(str(raw_amount).replace(",", ".").replace(" ", ""))
    except (TypeError, ValueError):
        return 0.0


def _date(transaction: dict) -> str:
    return str(transaction.get("date", "") or "")


def _type_matches(transaction: dict, wanted_type: str) -> bool:
    return str(transaction.get("type") or TYPE_EXPENSE) == wanted_type


def _build_pie_chart(
    transactions: list[dict],
    wanted_type: str,
    title_word: str,
    empty_text: str,
    output_path: Path,
) -> str:
    """Общая логика построения круговой диаграммы — расходов ИЛИ доходов
    (раньше существовала только версия для расходов, поэтому "график по
    доходам" молча показывал тот же самый график трат)."""
    now = datetime.datetime.now(ASTANA_TZ)
    current_month = now.strftime("%Y-%m")
    by_category: dict[str, float] = {}

    for transaction in transactions or []:
        if not _date(transaction).startswith(current_month):
            continue
        if not _type_matches(transaction, wanted_type):
            continue
        amount = _amount(transaction)
        if amount <= 0:
            continue
        category = str(
            transaction.get("cat", transaction.get("category", "Прочее"))
            or "Прочее"
        ).strip()
        by_category[category] = by_category.get(category, 0.0) + amount

    plt.close("all")
    figure, axis = plt.subplots(figsize=(9, 6), dpi=150)
    if by_category:
        labels = list(by_category)
        values = list(by_category.values())
        wedges, _, _ = axis.pie(
            values,
            autopct="%1.0f%%",
            startangle=90,
            textprops={"fontsize": 9},
        )
        axis.legend(
            wedges,
            [f"{label}: {value:,.0f} ₸" for label, value in zip(labels, values)],
            title="Категории",
            loc="center left",
            bbox_to_anchor=(1, 0.5),
            fontsize=8,
        )
        axis.set_title(f"{title_word} за {now.strftime('%m.%Y')}", fontsize=14)
    else:
        axis.text(0.5, 0.5, empty_text, ha="center", va="center", fontsize=13)
        axis.set_title(f"{title_word} за {now.strftime('%m.%Y')}", fontsize=14)
        axis.axis("off")

    figure.tight_layout()
    figure.savefig(output_path, format="png", bbox_inches="tight")
    plt.close(figure)
    return str(output_path)


def generate_spending_chart(transactions: list[dict]) -> str:
    """Сохранить круговую диаграмму РАСХОДОВ текущего месяца и вернуть путь.

    Доходные операции (type=ДОХОД) в диаграмму не попадают — иначе
    зарплата или возврат долга исказили бы структуру трат.
    """
    return _build_pie_chart(
        transactions, TYPE_EXPENSE, "Расходы", "Расходов за текущий месяц пока нет", CHART_FILE_EXPENSE
    )


def generate_income_chart(transactions: list[dict]) -> str:
    """Сохранить круговую диаграмму ДОХОДОВ текущего месяца и вернуть путь."""
    return _build_pie_chart(
        transactions, TYPE_INCOME, "Доходы", "Доходов за текущий месяц пока нет", CHART_FILE_INCOME
    )
