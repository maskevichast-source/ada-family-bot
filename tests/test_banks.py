import pytest

from services.banks import (
    canonical_bank_name,
    normalize_bank_source,
    KNOWN_SOURCES_BY_BANK,
    VALID_SOURCES_LOWER,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("bereke", "Bereke"),
        ("Береке", "Bereke"),
        ("евразийский", "Евразийский"),
        ("Eurasian Bank", "Евразийский"),
        ("bank rbk", "Bank RBK"),
        ("РБК", "Bank RBK"),
        ("нурбанк", "Нурбанк"),
        ("Nurbank", "Нурбанк"),
        ("altyn bank", "Altyn Bank"),
        ("Алтын", "Altyn Bank"),
        ("alatau city bank", "Alatau City Bank"),
        ("Алатау", "Alatau City Bank"),
        # старые банки не должны были сломаться
        ("kaspi red", "Kaspi"),
        ("ozen", "Home Credit"),
        ("halyk", "Halyk"),
    ],
)
def test_canonical_bank_name_recognizes_new_banks(raw, expected):
    assert canonical_bank_name(raw) == expected


def test_canonical_bank_name_unknown_returns_none():
    # Незнакомый банк -> None, чтобы вызывающий код переспросил,
    # а не тихо проигнорировал команду.
    assert canonical_bank_name("Совершенно Незнакомый Банк") is None


@pytest.mark.parametrize(
    "bank,expected_source",
    [
        ("bereke", "Bereke Card"),
        ("евразийский", "Евразийский Card"),
        ("bank rbk", "Bank RBK Card"),
        ("нурбанк", "Нурбанк Card"),
        ("altyn bank", "Altyn Card"),
        ("alatau city bank", "Alatau Card"),
    ],
)
def test_normalize_bank_source_new_banks(bank, expected_source):
    assert normalize_bank_source(bank, "") == expected_source


@pytest.mark.parametrize(
    "source",
    ["Картакарта", "ForteBlack", "Halyk Рассрочка", "SmartCard", "PayDa"],
)
def test_installment_sources_are_kept_as_is(source):
    # Валидные источники рассрочки не должны подменяться дефолтом.
    assert normalize_bank_source(None, source) == source
    assert source.lower() in VALID_SOURCES_LOWER


def test_unknown_bank_source_is_not_silently_discarded():
    # Раньше незнакомый банк/карта в source молча оставался как есть только
    # если source был непустым - эта регрессия защищает именно это поведение:
    # AI-промпт теперь тоже просит писать банк "как есть" вместо "Не указан",
    # так что здесь мы проверяем, что normalize_bank_source не портит и не
    # обнуляет такое сырое значение.
    assert normalize_bank_source("Новый Банк РК", "Новый Банк РК Card") == "Новый Банк РК Card"


def test_cash_never_maps_to_a_bank_name():
    assert normalize_bank_source("наличные", "") == "Основная карта"
    assert normalize_bank_source("нал", "") == "Основная карта"


def test_known_sources_by_bank_all_map_to_valid_sources():
    # Каждое значение из KNOWN_SOURCES_BY_BANK обязано быть в списке
    # валидных источников, иначе normalize_bank_source() вернёт то,
    # что потом снова не пройдёт проверку VALID_SOURCES_LOWER выше по коду.
    for bank, source in KNOWN_SOURCES_BY_BANK.items():
        assert source.lower() in VALID_SOURCES_LOWER, (
            f"{bank!r} -> {source!r} отсутствует в VALID_SOURCES_LOWER"
        )
