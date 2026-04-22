"""Feriados nacionais, estaduais (MT) e municipais (Cuiabá/Rondonópolis).

Usa lib `holidays` para feriados oficiais + customizados para datas móveis
(Carnaval, Corpus Christi) e municipais.

Baseado em: /var/www/agente-langgraph/core/agendamento/regras_horario.py
"""

from datetime import date

import holidays

# Dias que não constam na lib holidays (móveis ou municipais)
FERIADOS_CUSTOMIZADOS: dict = {
    # Carnaval 2026 (segunda e terça)
    date(2026, 2, 16): "Carnaval",
    date(2026, 2, 17): "Carnaval",
    # Corpus Christi 2026
    date(2026, 6, 4): "Corpus Christi",
    # Aniversário de Rondonópolis
    date(2026, 12, 10): "Aniversário de Rondonópolis",
    # Carnaval 2027
    date(2027, 2, 8): "Carnaval",
    date(2027, 2, 9): "Carnaval",
    # Corpus Christi 2027
    date(2027, 5, 27): "Corpus Christi",
    date(2027, 12, 10): "Aniversário de Rondonópolis",
}

_feriados_cache: dict = {}


def eh_feriado(dt: date):
    """Retorna nome do feriado ou None se dia útil.

    Verifica: customizados (Carnaval, Corpus Christi, municipal)
    + nacionais + estaduais (MT) via lib holidays.
    """
    custom = FERIADOS_CUSTOMIZADOS.get(dt)
    if custom:
        return custom
    year = dt.year
    if year not in _feriados_cache:
        _feriados_cache[year] = holidays.Brazil(
            state="MT", years=year, language="pt_BR"
        )
    return _feriados_cache[year].get(dt)
