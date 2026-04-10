"""Testes unitários do client Leadbox (infra/leadbox_client.py).

Valida envio de resposta, prefixo *Ana:*, tratamento de erros,
e chamada ao marker anti-eco.
"""

from unittest.mock import patch, MagicMock

from infra.leadbox_client import enviar_resposta_leadbox, AGENT_NAME


def test_prefixo_ana_no_payload():
    """Mensagem enviada deve ter prefixo '*Ana:*\\n'."""
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_client = MagicMock()
    mock_client.post = MagicMock(return_value=mock_resp)

    with patch("infra.leadbox_client.httpx.Client") as MockClient, \
         patch("infra.leadbox_client._mark_sent_by_ia"), \
         patch("infra.leadbox_client.LEADBOX_API_TOKEN", "fake_token"):
        MockClient.return_value.__enter__ = MagicMock(return_value=mock_client)
        MockClient.return_value.__exit__ = MagicMock(return_value=False)

        result = enviar_resposta_leadbox("5565999990000", "Olá!")

    payload = mock_client.post.call_args.kwargs.get("json") or mock_client.post.call_args[1].get("json")
    assert payload["body"].startswith(f"*{AGENT_NAME}:*\n")
    assert result is True


def test_retorna_false_sem_token():
    """Sem LEADBOX_API_TOKEN, deve retornar False."""
    with patch("infra.leadbox_client.LEADBOX_API_TOKEN", ""):
        result = enviar_resposta_leadbox("5565999990000", "Olá!")
    assert result is False


def test_retorna_false_em_erro_http():
    """Erro HTTP deve retornar False e registrar incidente."""
    with patch("infra.leadbox_client.httpx.Client") as MockClient:
        mock_client = MagicMock()
        mock_client.post.side_effect = Exception("Connection refused")
        MockClient.return_value.__enter__ = MagicMock(return_value=mock_client)
        MockClient.return_value.__exit__ = MagicMock(return_value=False)

        with patch("infra.leadbox_client.registrar_incidente", create=True):
            result = enviar_resposta_leadbox("5565999990000", "Olá!")

    assert result is False


def test_payload_contem_external_key():
    """Payload deve conter externalKey igual ao phone."""
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_client = MagicMock()
    mock_client.post = MagicMock(return_value=mock_resp)

    with patch("infra.leadbox_client.httpx.Client") as MockClient, \
         patch("infra.leadbox_client._mark_sent_by_ia"), \
         patch("infra.leadbox_client.LEADBOX_API_TOKEN", "fake_token"):
        MockClient.return_value.__enter__ = MagicMock(return_value=mock_client)
        MockClient.return_value.__exit__ = MagicMock(return_value=False)

        enviar_resposta_leadbox("5565999990000", "Teste")

    payload = mock_client.post.call_args.kwargs.get("json") or mock_client.post.call_args[1].get("json")
    assert payload["externalKey"] == "5565999990000"
    assert payload["number"] == "5565999990000"


def test_token_como_query_param():
    """Token deve ser enviado como query param, não header Bearer."""
    with patch("infra.leadbox_client.httpx.Client") as MockClient:
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.post = MagicMock(return_value=mock_resp)
        MockClient.return_value.__enter__ = MagicMock(return_value=mock_client)
        MockClient.return_value.__exit__ = MagicMock(return_value=False)

        with patch("infra.leadbox_client._mark_sent_by_ia"), \
             patch("infra.leadbox_client.LEADBOX_API_TOKEN", "test_jwt_token"):
            enviar_resposta_leadbox("5565999990000", "Teste")

        call_args = mock_client.post.call_args
        params = call_args.kwargs.get("params") or call_args[1].get("params")
        assert params == {"token": "test_jwt_token"}


def _make_mock_client():
    """Helper: cria mock httpx.Client com POST bem-sucedido."""
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_client = MagicMock()
    mock_client.post = MagicMock(return_value=mock_resp)
    return mock_client


def _get_payload(mock_client):
    """Helper: extrai payload JSON do POST mockado."""
    return mock_client.post.call_args.kwargs.get("json") or mock_client.post.call_args[1].get("json")


def test_payload_com_queue_e_user_id():
    """Payload com queue_id e user_id deve incluir queueId, userId e forceTicket flags."""
    mock_client = _make_mock_client()

    with patch("infra.leadbox_client.httpx.Client") as MockClient, \
         patch("infra.leadbox_client._mark_sent_by_ia"), \
         patch("infra.leadbox_client.LEADBOX_API_TOKEN", "fake_token"):
        MockClient.return_value.__enter__ = MagicMock(return_value=mock_client)
        MockClient.return_value.__exit__ = MagicMock(return_value=False)

        enviar_resposta_leadbox("5565999990000", "Oi", queue_id=537, user_id=1095)

    payload = _get_payload(mock_client)
    assert payload["queueId"] == 537
    assert payload["userId"] == 1095
    assert payload["forceTicketToDepartment"] is True
    assert payload["forceTicketToUser"] is True


def test_payload_sem_queue_e_user_id():
    """Payload sem params opcionais NÃO deve conter queueId/userId (retrocompat)."""
    mock_client = _make_mock_client()

    with patch("infra.leadbox_client.httpx.Client") as MockClient, \
         patch("infra.leadbox_client._mark_sent_by_ia"), \
         patch("infra.leadbox_client.LEADBOX_API_TOKEN", "fake_token"):
        MockClient.return_value.__enter__ = MagicMock(return_value=mock_client)
        MockClient.return_value.__exit__ = MagicMock(return_value=False)

        enviar_resposta_leadbox("5565999990000", "Oi")

    payload = _get_payload(mock_client)
    assert "queueId" not in payload
    assert "userId" not in payload
    assert "forceTicketToDepartment" not in payload
    assert "forceTicketToUser" not in payload


def test_payload_com_queue_sem_user():
    """Payload com queue_id mas sem user_id deve incluir só queueId."""
    mock_client = _make_mock_client()

    with patch("infra.leadbox_client.httpx.Client") as MockClient, \
         patch("infra.leadbox_client._mark_sent_by_ia"), \
         patch("infra.leadbox_client.LEADBOX_API_TOKEN", "fake_token"):
        MockClient.return_value.__enter__ = MagicMock(return_value=mock_client)
        MockClient.return_value.__exit__ = MagicMock(return_value=False)

        enviar_resposta_leadbox("5565999990000", "Oi", queue_id=537)

    payload = _get_payload(mock_client)
    assert payload["queueId"] == 537
    assert payload["forceTicketToDepartment"] is True
    assert "userId" not in payload
    assert "forceTicketToUser" not in payload
