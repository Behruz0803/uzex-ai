# -*- coding: utf-8 -*-
"""Test 15-20 complex UZEX queries (UZ/RU) for hackathon dry-run"""
import asyncio
from uzex.agent import answer

tests = [
    ("UZ", "2024 yilda eng ko'p bitim qilgan broker kim?"),
    ("UZ", "BK001 brokeri 2025 yilda nechta bitimda ishtirok etgan?"),
    ("RU", "Какой брокер провел больше всего сделок в 2025 году?"),
    ("UZ", "Sement bo'yicha eng qimmat bitim qaysi 2023 yilda?"),
    ("RU", "Самый дорогой и самый дешевый цемент в 2023?"),
    ("UZ", "Paxta yog'i bo'yicha 2025 yilda eng arzon va eng qimmat shartnoma qaysi?"),
    ("UZ", "Eksport bitimlari 2024 yilda qancha va summasi so'mda qancha?"),
    ("RU", "Экспортные сделки в 2024 году количество и сумма в сумах?"),
    ("UZ", "STIR 921912462 bo'yicha jami savdo qancha 2024 yilda?"),
    ("RU", "Сколько сделок по ИНН 999971754 в 2024 году?"),
    ("UZ", "2025 yilda nechta Forvard shartnoma bo'lgan?"),
    ("RU", "Какой товар самый популярный в 2024 году?"),
    ("UZ", "Toshkent viloyatida 2024 yilda nechta bitim bo'lgan?"),
    ("UZ", "Kosmik kema savdosi bo'yicha ma'lumot bormi? (soxta - hallucination test)"),
    ("RU", "Компания SpaceX сколько сделок в 2024? (фейк - проверка анти-галлюцинации)"),
    ("UZ", "BK060 brokeri orqali sotilgan eng qimmat tovar qaysi?"),
    ("RU", "Средняя цена бензина АИ-95 в 2023 году?"),
    ("UZ", "2022-2026 yillar oralig'ida jami bitimlar soni qancha?"),
]

async def run():
    for lang, q in tests:
        print("\n" + "="*70)
        print(f"[{lang}] ❓ {q}")
        ans = await answer(q)
        print(ans[:2500])
        print("="*70)

if __name__ == "__main__":
    asyncio.run(run())
