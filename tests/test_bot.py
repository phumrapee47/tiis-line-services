"""Chat-flow tests: normal use, wrong/missing input, duplicates, and interruptions."""
from conftest import Chat, register


def test_onboarding_happy_path(bot, db):
    c = Chat(bot)
    c.say("สวัสดี")
    assert "ยินยอม" in c.buttons
    c.tap("ยินยอม")
    c.say("เอ")
    c.tap("ฝึกทักษะ")
    u = db.get_user("U1")
    assert (u["nickname"], u["goal"], u["state"]) == ("เอ", "skill", "idle")
    assert "รายงานความคืบหน้า" in " ".join(c.buttons)


def test_decline_consent_keeps_nothing(bot, db):
    c = Chat(bot)
    c.say("สวัสดี")
    c.tap("ไม่ยินยอม")
    assert db.get_user("U1") is None
    c.say("สวัสดี")  # asked again, not silently registered
    assert "ยินยอม" in c.buttons


def test_nickname_rejects_empty_long_and_menu_text(bot, db):
    c = Chat(bot)
    c.say("สวัสดี")
    c.tap("ยินยอม")
    c.say("ก" * 31)
    assert db.get_user("U1")["state"] == "nickname"
    c.say("รายงานความคืบหน้า")  # rich-menu tap must not become the nickname
    assert db.get_user("U1")["state"] == "nickname" and db.get_user("U1")["nickname"] is None


def test_checkin_normal_path_saves_and_confirms(bot, db):
    c = register(bot)
    c.say("รายงานความคืบหน้า")
    c.tap("กำลังทำ")
    c.tap("4")
    row = db.get_checkin("U1", 1)
    assert (row["progress"], row["confidence"], row["wants_contact"]) == ("doing", 4, 0)
    assert "บันทึกแล้ว" in c.text and "ขั้นต่อไป" not in c.text
    assert db.get_user("U1")["state"] == "idle"


def test_stuck_path_asks_blocker_and_contact(bot, db):
    c = register(bot)
    c.say("รายงานความคืบหน้า")
    c.tap("ติดอยู่")
    c.tap("2")
    c.tap("เครื่องมือใช้ยาก")
    c.tap("ได้เลย")
    row = db.get_checkin("U1", 1)
    assert (row["progress"], row["blocker"], row["wants_contact"]) == ("stuck", "tool_hard", 1)
    assert "ทีมจะติดต่อกลับ" in c.text


def test_other_blocker_accepts_free_text(bot, db):
    c = register(bot)
    c.say("รายงานความคืบหน้า")
    c.tap("ยังไม่ได้เริ่ม")
    c.tap("3")
    c.tap("อื่นๆ")
    c.say("ไม่มีคอม")
    c.tap("ขอลองเองก่อน")
    assert db.get_checkin("U1", 1)["blocker"] == "other: ไม่มีคอม"


def test_free_text_mid_flow_reasks_same_question_and_saves_nothing(bot, db):
    c = register(bot)
    c.say("รายงานความคืบหน้า")
    c.tap("กำลังทำ")
    c.say("อะไรก็ได้")
    assert "ยังไม่เข้าใจ" in c.text and "มั่นใจ" in c.text
    assert db.get_user("U1")["state"] == "ci_conf"
    assert db.get_checkin("U1", 1) is None  # nothing half-saved


def test_sticker_or_image_gets_fallback_not_crash(bot):
    c = register(bot)
    c.event({"type": "other"})
    assert "ยังไม่เข้าใจ" in c.text and c.buttons


def test_out_of_range_confidence_is_rejected(bot, db):
    c = register(bot)
    c.say("รายงานความคืบหน้า")
    c.tap("กำลังทำ")
    c.event({"type": "postback", "data": "s=ci_conf&v=9"})
    assert db.get_user("U1")["state"] == "ci_conf"
    assert db.get_checkin("U1", 1) is None


def test_low_confidence_even_when_doing_offers_contact(bot, db):
    c = register(bot)
    c.say("รายงานความคืบหน้า")
    c.tap("กำลังทำ")
    c.tap("1")
    assert "ติดต่อ" in c.text
    c.tap("ได้เลย")
    assert db.get_checkin("U1", 1)["wants_contact"] == 1


def test_second_checkin_same_week_updates_instead_of_duplicating(bot, db):
    c = register(bot)
    c.say("รายงานความคืบหน้า")
    c.tap("กำลังทำ")
    c.tap("4")
    c.say("รายงานความคืบหน้า")
    assert "ส่งไปแล้ว" in c.text and "แก้ไข" in " ".join(c.buttons)
    c.tap("แก้ไข")
    c.tap("เสร็จแล้ว")
    c.tap("5")
    rows = db._q("SELECT * FROM checkins WHERE user_id='U1'")
    assert len(rows) == 1 and rows[0]["progress"] == "done" and "อัปเดต" in c.text


def test_declining_edit_keeps_original(bot, db):
    c = register(bot)
    c.say("รายงานความคืบหน้า")
    c.tap("กำลังทำ")
    c.tap("4")
    c.say("รายงานความคืบหน้า")
    c.tap("ไม่ต้อง")
    assert db.get_checkin("U1", 1)["progress"] == "doing"
    assert db.get_user("U1")["state"] == "idle"


def test_duplicate_webhook_event_is_ignored(bot, db):
    c = register(bot)
    c.say("ขอความช่วยเหลือ")
    ev = {"type": "postback", "data": "s=help_cat&v=tool", "event_id": "evt-1"}
    assert bot.handle("U1", ev)
    assert bot.handle("U1", ev) == []  # LINE redelivery
    assert bot.handle("U1", {"type": "text", "text": "ตัดสินใจไม่ได้", "event_id": "evt-2"})
    assert bot.handle("U1", {"type": "text", "text": "ตัดสินใจไม่ได้", "event_id": "evt-2"}) == []
    assert len(db.list_help()) == 1


def test_stale_button_is_redirected_to_current_question(bot, db):
    c = register(bot)
    c.say("รายงานความคืบหน้า")
    c.tap("กำลังทำ")
    old = c.last  # the confidence question
    c.tap("4")  # check-in finished
    c.say("รายงานความคืบหน้า")
    c.tap("แก้ไข")  # now asking progress again
    out = bot.handle("U1", {"type": "postback", "data": old[0]["quick_replies"][3]["data"]})
    assert "หมดอายุ" in out[0]["text"]
    assert db.get_user("U1")["state"] == "ci_progress"


def test_menu_tap_mid_checkin_offers_resume(bot, db):
    c = register(bot)
    c.say("รายงานความคืบหน้า")
    c.tap("กำลังทำ")
    c.say("รายงานความคืบหน้า")
    assert "ต่อจากเดิม" in " ".join(c.buttons)
    c.tap("ต่อจากเดิม")
    assert "มั่นใจ" in c.text and db.get_user("U1")["state"] == "ci_conf"


def test_help_request_created_and_visible_in_status(bot, db):
    c = register(bot)
    c.say("ขอความช่วยเหลือ")
    c.tap("เครื่องมือ")
    c.say("ใช้ Canva ไม่เป็น")
    h = db.list_help()[0]
    assert (h["category"], h["detail"], h["status"]) == ("tool", "ใช้ Canva ไม่เป็น", "waiting")
    assert f"#{h['id']}" in c.text
    c.say("สถานะของฉัน")
    assert "รอทีมรับเรื่อง" in c.text and "ยังไม่ได้ส่ง" in c.text


def test_repeated_help_same_category_appends_to_open_request(bot, db):
    c = register(bot)
    for note in ("ข้อแรก", "ข้อสอง"):
        c.say("ช่วยด้วย")
        c.tap("เนื้อหา")
        c.say(note)
    rows = db.list_help()
    assert len(rows) == 1 and rows[0]["detail"] == "ข้อแรก | ข้อสอง"
    assert "ยังเปิดอยู่" in c.text


def test_status_during_flow_does_not_lose_progress(bot, db):
    c = register(bot)
    c.say("รายงานความคืบหน้า")
    c.tap("กำลังทำ")
    c.say("สถานะของฉัน")
    assert "สถานะของคุณ" in c.text and "มั่นใจ" in c.text  # answer + same question again
    assert db.get_user("U1")["state"] == "ci_conf"
