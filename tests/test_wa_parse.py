"""WhatsApp webhook payload parsing — shapes from Meta's payload examples."""
from bot.whatsapp.parse import parse_envelopes

NUM = "306912345678"


def wa_body(messages, contacts=None):
    return {
        "object": "whatsapp_business_account",
        "entry": [{"id": "WABA-ID", "changes": [{
            "field": "messages",
            "value": {
                "messaging_product": "whatsapp",
                "metadata": {"display_phone_number": "3021", "phone_number_id": "111"},
                "contacts": contacts if contacts is not None else [
                    {"wa_id": NUM, "profile": {"name": "Alex"}}],
                "messages": messages,
            },
        }]}],
    }


def text_msg(body, mid="wamid.A1", frm=NUM):
    return {"from": frm, "id": mid, "timestamp": "1722945600", "type": "text",
            "text": {"body": body}}


def test_a_text_message_parses():
    [ctx] = parse_envelopes(wa_body([text_msg("hello there")]))
    assert ctx.text == "hello there"
    assert ctx.chat_id == NUM
    assert ctx.event_id == "wamid.A1"
    assert ctx.sender_name == "Alex"
    assert ctx.command is None
    assert ctx.is_private


def test_chat_ids_never_reach_logs_raw():
    [ctx] = parse_envelopes(wa_body([text_msg("hi")]))
    assert ctx.log_ref != ctx.chat_id
    assert NUM not in ctx.log_ref


def test_link_keyword_is_case_insensitive_but_the_code_is_verbatim():
    [ctx] = parse_envelopes(wa_body([text_msg("LiNk AbC-123_xyz")]))
    assert ctx.command == "start"
    assert ctx.args == "AbC-123_xyz"  # token_urlsafe codes are case-sensitive


def test_a_bare_code_without_the_keyword_is_not_a_link_attempt():
    [ctx] = parse_envelopes(wa_body([text_msg("AbC-123_xyz")]))
    assert ctx.command is None


def test_slash_commands_still_work():
    [ctx] = parse_envelopes(wa_body([text_msg("/agent Freight Desk")]))
    assert (ctx.command, ctx.args) == ("agent", "Freight Desk")


def test_single_bare_words_are_commands():
    for word in ("agents", "NEW", "Status", "unlink", "help", "menu"):
        [ctx] = parse_envelopes(wa_body([text_msg(word)]))
        assert ctx.command == word.lower(), word


def test_multi_word_text_is_never_swallowed_as_a_command():
    [ctx] = parse_envelopes(wa_body([text_msg("new fixture for MV OCEAN STAR")]))
    assert ctx.command is None
    assert ctx.text == "new fixture for MV OCEAN STAR"


def test_button_and_list_replies_carry_the_codec_id():
    msgs = [
        {"from": NUM, "id": "wamid.B1", "type": "interactive",
         "interactive": {"type": "button_reply",
                         "button_reply": {"id": "1:w:y:p1", "title": "✅ Approve"}}},
        {"from": NUM, "id": "wamid.L1", "type": "interactive",
         "interactive": {"type": "list_reply",
                         "list_reply": {"id": "1:a:abcdef", "title": "Freight Desk"}}},
    ]
    a, b = parse_envelopes(wa_body(msgs))
    assert a.callback_data == "1:w:y:p1" and a.callback_id == "wamid.B1"
    assert b.callback_data == "1:a:abcdef"


def test_media_asks_for_text_and_reactions_are_ignored():
    msgs = [
        {"from": NUM, "id": "wamid.M1", "type": "image",
         "image": {"id": "media1", "mime_type": "image/jpeg", "caption": "a photo of a Q88"}},
        {"from": NUM, "id": "wamid.R1", "type": "reaction",
         "reaction": {"message_id": "wamid.A1", "emoji": "👍"}},
    ]
    envs = parse_envelopes(wa_body(msgs))
    assert len(envs) == 1  # the reaction produced nothing
    assert envs[0].is_unsupported_media  # a photo of a file is still a photo
    assert envs[0].attachment is None and envs[0].media_kind == "image"


def test_a_batch_yields_one_envelope_per_message():
    msgs = [text_msg(f"m{i}", mid=f"wamid.{i}") for i in range(3)]
    envs = parse_envelopes(wa_body(msgs))
    assert [e.event_id for e in envs] == ["wamid.0", "wamid.1", "wamid.2"]


def test_statuses_only_payloads_yield_nothing():
    body = {
        "object": "whatsapp_business_account",
        "entry": [{"id": "WABA-ID", "changes": [{
            "field": "messages",
            "value": {"messaging_product": "whatsapp",
                      "metadata": {"phone_number_id": "111"},
                      "statuses": [{"id": "wamid.OUT", "status": "delivered",
                                    "recipient_id": NUM}]},
        }]}],
    }
    assert parse_envelopes(body) == []


def test_non_whatsapp_objects_yield_nothing():
    assert parse_envelopes({"object": "page", "entry": []}) == []


# ---------------------------------------------------------------------------
# review findings, pinned
# ---------------------------------------------------------------------------
def test_unsupported_content_gets_the_text_only_nudge_not_silence():
    """type=unsupported means the user actively sent something (a poll,
    view-once media) — they must hear the bot is text-only."""
    msg = {"from": NUM, "id": "wamid.U1", "type": "unsupported",
           "errors": [{"code": 131051, "title": "Message type unknown"}]}
    [ctx] = parse_envelopes(wa_body([msg]))
    assert ctx.is_unsupported_media


def test_changes_for_another_phone_number_are_skipped():
    body = wa_body([text_msg("hello")])
    assert parse_envelopes(body, "111") != []       # matches the fixture metadata
    assert parse_envelopes(body, "999") == []       # another number's traffic
    assert parse_envelopes(body) != []              # no filter when unset


def test_empty_and_whitespace_bodies_produce_no_envelope():
    """'' answered nothing after an ack; '  ' burned a real agent turn."""
    assert parse_envelopes(wa_body([text_msg("")])) == []
    assert parse_envelopes(wa_body([text_msg("   ")])) == []


# ---------------------------------------------------------------------------
# documents (NAV-81) — read, not refused
# ---------------------------------------------------------------------------
def doc_msg(caption=None, mid="wamid.D1", **over):
    document = {"id": "1234567890", "filename": "Recap MV OCEAN STAR.docx",
                "mime_type": "application/vnd.openxmlformats-officedocument"
                             ".wordprocessingml.document",
                "sha256": "abc123", **over}
    if caption is not None:
        document["caption"] = caption
    return {"from": NUM, "id": mid, "timestamp": "1722945600", "type": "document",
            "document": document}


def test_a_document_becomes_an_attachment_with_its_caption_and_name():
    [ctx] = parse_envelopes(wa_body([doc_msg("short desc please")]))
    assert not ctx.is_unsupported_media
    assert ctx.text == "short desc please"
    att = ctx.attachment
    assert (att.ref, att.file_name, att.sha256) == ("1234567890", "Recap MV OCEAN STAR.docx", "abc123")
    assert att.mime_hint.endswith("wordprocessingml.document")
    assert att.size_hint is None  # the webhook doesn't say; get_media does


def test_a_document_without_a_caption_still_arrives():
    for caption in (None, "", "   "):
        [ctx] = parse_envelopes(wa_body([doc_msg(caption)]))
        assert ctx.attachment is not None
        assert ctx.text is None


def test_a_document_caption_is_never_a_command():
    """'new' under a recap is about the recap; 'LINK x' under a file is not a
    pairing attempt."""
    for caption in ("new", "help", "/agents", "LINK AbC-123"):
        [ctx] = parse_envelopes(wa_body([doc_msg(caption)]))
        assert (ctx.command, ctx.args) == (None, ""), caption
        assert ctx.text == caption


def test_a_document_without_a_media_id_is_refused_rather_than_dropped():
    [ctx] = parse_envelopes(wa_body([doc_msg(id=None)]))
    assert ctx.attachment is None
    assert ctx.is_unsupported_media
