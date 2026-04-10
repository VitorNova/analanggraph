"""
Teste de detecção fromMe — roda 52+ payloads REAIS capturados em produção.

Verifica que:
1. sendType="API" → classificado como IA → NÃO pausa
2. sendType="chat" → classificado como HUMANO → pausa
3. sendType=None → classificado como HUMANO → pausa (caso conservador)
4. Marker Redis presente → classificado como IA → NÃO pausa (independente de sendType)

Roda SEM Redis/Supabase — testa apenas a lógica de classificação.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PAYLOADS_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs", "webhook_payloads.jsonl")


def classify_fromme(message: dict, has_redis_marker: bool = False) -> str:
    """
    Reproduz a lógica NOVA do webhook para classificar fromMe.

    Returns:
        "ia_echo"       — marker Redis presente
        "ia_echo_api"   — sendType=API (IA via API)
        "human"         — humano real (pausa IA)
    """
    if has_redis_marker:
        return "ia_echo"

    send_type = message.get("sendType")
    if send_type == "API":
        return "ia_echo_api"

    return "human"


def classify_fromme_OLD(message: dict, ticket: dict, has_redis_marker: bool = False) -> str:
    """
    Reproduz a lógica ANTIGA (com check IA_QUEUES) para comparação.
    """
    IA_QUEUES = {537, 544, 545}

    if has_redis_marker:
        return "ia_echo"

    ticket_queue = ticket.get("queueId")
    if ticket_queue and ticket_queue in IA_QUEUES:
        return "ia_echo_queue"  # ← BUG: ignora humano real

    return "human"


def load_fromme_payloads():
    """Carrega todos os payloads fromMe do arquivo de captura."""
    if not os.path.exists(PAYLOADS_FILE):
        return []

    payloads = []
    with open(PAYLOADS_FILE, "r", encoding="utf-8") as f:
        for line in f:
            try:
                entry = json.loads(line.strip())
                raw = entry.get("raw", entry)
                if raw.get("event") != "NewMessage":
                    continue
                msg = raw.get("message", {}) or {}
                if not msg.get("fromMe"):
                    continue
                ticket = msg.get("ticket", {}) or {}
                contact = ticket.get("contact", {}) or {}
                payloads.append({
                    "phone": contact.get("number", "?"),
                    "sendType": msg.get("sendType"),
                    "userId": msg.get("userId"),
                    "ticket_userId": ticket.get("userId"),
                    "queueId": ticket.get("queueId"),
                    "body": (msg.get("body") or "")[:80],
                    "message": msg,
                    "ticket": ticket,
                })
            except (json.JSONDecodeError, KeyError):
                continue
    return payloads


def test_real_payloads():
    """Testa TODOS os payloads fromMe reais contra a lógica nova vs antiga."""
    payloads = load_fromme_payloads()
    assert len(payloads) > 0, f"Nenhum payload fromMe encontrado em {PAYLOADS_FILE}"

    results = {"ia_echo_api": 0, "human": 0, "ia_echo": 0}
    bugs_old_logic = []
    errors = []

    for i, p in enumerate(payloads):
        # Classificação NOVA (sem marker, como seria em produção)
        new_result = classify_fromme(p["message"], has_redis_marker=False)
        results[new_result] += 1

        # Classificação ANTIGA para comparar
        old_result = classify_fromme_OLD(p["message"], p["ticket"], has_redis_marker=False)

        # Validar contra ground truth:
        # - sendType="API" → deve ser IA
        # - sendType="chat" → deve ser humano
        # - sendType=None com userId=None → caso ambíguo, tratar como humano (conservador)
        send_type = p["sendType"]
        user_id = p["userId"]

        if send_type == "API":
            # IA enviando via API
            if new_result != "ia_echo_api":
                errors.append(f"Payload {i}: sendType=API mas classificado como {new_result}")
        elif send_type == "chat":
            # Humano enviando pelo painel
            if new_result != "human":
                errors.append(f"Payload {i}: sendType=chat (humano) mas classificado como {new_result}")
            # Verificar se a lógica antiga teria BUG aqui
            queue_id = p["queueId"]
            if old_result == "ia_echo_queue":
                bugs_old_logic.append(
                    f"Payload {i}: phone={p['phone'][-4:]}, queue={queue_id}, "
                    f"user={user_id} — ANTIGA ignoraria humano real!"
                )
        elif send_type is None:
            # Caso ambíguo — nova lógica trata como humano (conservador)
            if new_result != "human":
                errors.append(f"Payload {i}: sendType=None mas classificado como {new_result}")

    print(f"\n{'='*60}")
    print(f"TESTE fromMe — {len(payloads)} payloads reais de produção")
    print(f"{'='*60}")
    print(f"  Classificação nova:")
    print(f"    IA (sendType=API):  {results['ia_echo_api']}")
    print(f"    IA (marker Redis):  {results['ia_echo']}")
    print(f"    HUMANO (pausa IA):  {results['human']}")
    print(f"  Bugs da lógica ANTIGA: {len(bugs_old_logic)}")
    for b in bugs_old_logic:
        print(f"    ⚠ {b}")
    print(f"  Erros na lógica NOVA: {len(errors)}")
    for e in errors:
        print(f"    ✗ {e}")
    print(f"{'='*60}")

    assert len(errors) == 0, f"{len(errors)} erros na classificação nova"
    print(f"  ✓ TODOS os {len(payloads)} payloads classificados corretamente")
    return True


def test_marker_always_wins():
    """Se marker Redis existe, SEMPRE é IA — independente de sendType."""
    cases = [
        {"sendType": "API", "userId": None},
        {"sendType": "chat", "userId": 815},
        {"sendType": None, "userId": None},
        {"sendType": "chat", "userId": 1090},
    ]
    for msg in cases:
        result = classify_fromme(msg, has_redis_marker=True)
        assert result == "ia_echo", f"Marker presente mas resultado={result} para {msg}"
    print("  ✓ Marker Redis sempre vence (4/4 casos)")


def test_sendtype_api_is_ia():
    """sendType=API sem marker → IA."""
    result = classify_fromme({"sendType": "API", "userId": None}, has_redis_marker=False)
    assert result == "ia_echo_api"
    print("  ✓ sendType=API sem marker → ia_echo_api")


def test_sendtype_chat_is_human():
    """sendType=chat sem marker → humano → pausa."""
    result = classify_fromme({"sendType": "chat", "userId": 815}, has_redis_marker=False)
    assert result == "human"
    print("  ✓ sendType=chat sem marker → human")


def test_sendtype_none_is_human():
    """sendType=None sem marker → humano (conservador)."""
    result = classify_fromme({"sendType": None, "userId": None}, has_redis_marker=False)
    assert result == "human"
    print("  ✓ sendType=None sem marker → human (conservador)")


def test_old_logic_would_miss_human_in_ia_queue():
    """Prova que a lógica ANTIGA ignoraria humano real na fila 544."""
    msg = {"sendType": "chat", "userId": 815}
    ticket = {"queueId": 544}
    old = classify_fromme_OLD(msg, ticket, has_redis_marker=False)
    new = classify_fromme(msg, has_redis_marker=False)
    assert old == "ia_echo_queue", f"Lógica antiga deveria ignorar, got {old}"
    assert new == "human", f"Lógica nova deveria pausar, got {new}"
    print("  ✓ Lógica antiga IGNORARIA humano na fila 544 — lógica nova PAUSA corretamente")


if __name__ == "__main__":
    print("\nTestes de detecção fromMe")
    print("=" * 60)

    test_marker_always_wins()
    test_sendtype_api_is_ia()
    test_sendtype_chat_is_human()
    test_sendtype_none_is_human()
    test_old_logic_would_miss_human_in_ia_queue()
    test_real_payloads()

    print("\n✓ TODOS OS TESTES PASSARAM\n")
