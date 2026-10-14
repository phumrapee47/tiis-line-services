"""Thai copy and message builders. A message is {"type":"text","text":..., "quick_replies":[...]};
a quick reply is {"label":..., "data": "s=<step>&v=<value>"} (sent back as a LINE postback).
Labels stay <= 20 chars (LINE limit)."""
from .config import SLA_TEXT

PROGRESS = {"done": "✅ ทำเสร็จแล้ว", "doing": "🔄 กำลังทำ",
            "not_started": "⏸ ยังไม่ได้เริ่ม", "stuck": "😣 ติดอยู่"}
CONF_LABELS = {1: "😟 1", 2: "😕 2", 3: "😐 3", 4: "🙂 4", 5: "😄 5"}
BLOCKERS = {"dont_know_start": "ไม่รู้จะเริ่มตรงไหน", "no_time": "ไม่มีเวลา",
            "tool_hard": "เครื่องมือใช้ยาก", "not_confident": "ไม่มั่นใจ", "other": "อื่นๆ (พิมพ์)"}
HELP_CATS = {"content": "เนื้อหา/ชิ้นงาน", "tool": "เครื่องมือ/เทคนิค",
             "confidence": "ไม่มั่นใจ/กำลังใจ", "other": "เรื่องอื่นๆ"}
GOALS = {"resume": "ทำ Resume/Portfolio", "skill": "ฝึกทักษะที่ใช้ทำงาน",
         "interview": "เตรียมสัมภาษณ์", "unsure": "ยังไม่แน่ใจ"}
HELP_STATUS = {"waiting": "รอทีมรับเรื่อง", "accepted": "ทีมรับเรื่องแล้ว",
               "replied": "ทีมตอบแล้ว", "closed": "ปิดเรื่อง"}
FOLLOWUP = {"used": "✅ ใช้แล้ว", "not_yet": "ยังไม่ได้ใช้", "stuck": "ลองแล้วติดปัญหา"}


def qr(label, step, value):
    return {"label": label, "data": f"s={step}&v={value}"}


def text(t, quick=None):
    return {"type": "text", "text": t, "quick_replies": quick or []}


def _menu_quick():
    return [qr("📝 รายงานความคืบหน้า", "idle", "checkin"),
            qr("🆘 ขอความช่วยเหลือ", "idle", "help")]


# onboarding
def consent():
    return text("สวัสดี 👋 ยินดีต้อนรับ!\nแชทนี้ช่วยให้รายงานความคืบหน้าและขอความช่วยเหลือจากทีมได้ง่ายๆ "
                "ใช้เวลาไม่ถึงนาที\n\nเราจะเก็บชื่อเล่น เป้าหมาย และคำตอบที่ส่งมา เพื่อให้ทีมช่วยได้ตรงจุด "
                "ใช้เฉพาะในโครงการนี้เท่านั้น ยินยอมไหม?",
                [qr("ยินยอม", "consent", "yes"), qr("ไม่ยินยอม", "consent", "no")])


def declined():
    return text("ไม่เป็นไรนะ เราไม่ได้เก็บข้อมูลของคุณไว้ ถ้าเปลี่ยนใจ ส่งข้อความมาได้ทุกเมื่อ")


def ask_nickname(note=""):
    return text(note + "ขอบคุณ 🙏 ให้เราเรียกคุณว่าอะไรดี? พิมพ์ชื่อเล่นได้เลย")


def ask_goal():
    return text("ช่วงนี้อยากพัฒนาเรื่องไหนเป็นหลัก?", [qr(v, "goal", k) for k, v in GOALS.items()])


def onboarded(nickname):
    return text(f"พร้อมแล้ว {nickname}! 🎉 เมื่อมีความคืบหน้า กดปุ่ม 'รายงานความคืบหน้า' ด้านล่างได้เลย "
                "ถ้าติดอะไร กด 'ขอความช่วยเหลือ' ได้ทุกเวลา", _menu_quick())


def welcome_back(nickname):
    return text(f"ยินดีต้อนรับกลับ {nickname} 👋 เลือกจากเมนูด้านล่างได้เลย", _menu_quick())


# check-in
def task_line(week, task):
    """'งานสัปดาห์ที่ N: หัวข้อ' plus the description; empty when the team set no task (weeks 5-6)."""
    if not task:
        return ""
    desc = f"\n{task['description']}" if task.get("description") else ""
    return f"📌 งานสัปดาห์ที่ {week}: {task['title']}{desc}\n\n"


def ask_progress(week, task=None):
    if task:
        head = f"{task_line(week, task)}ความคืบหน้าของงานนี้เป็นยังไงบ้าง?"
    else:
        head = f"สัปดาห์ที่ {week} เป็นยังไงบ้าง?"
    return text(head, [qr(v, "ci_progress", k) for k, v in PROGRESS.items()])


def ask_conf():
    return text("ตอนนี้มั่นใจแค่ไหน? (1 = ไม่มั่นใจเลย, 5 = มั่นใจมาก)",
                [qr(v, "ci_conf", str(k)) for k, v in CONF_LABELS.items()])


def ask_blocker():
    return text("ขอบคุณที่บอกนะ ไม่เป็นไรเลย 🙂 ตอนนี้ติดเรื่องอะไรเป็นหลัก?",
                [qr(v, "ci_blocker", k) for k, v in BLOCKERS.items()])


def ask_blocker_text():
    return text("เล่าสั้นๆ ได้ไหมว่าติดเรื่องอะไร? (พิมพ์ได้เลย)", [qr("ข้าม", "ci_blocker_text", "skip")])


def ask_contact():
    return text("อยากให้ทีมติดต่อไปช่วยไหม?",
                [qr("ได้เลย", "ci_contact", "yes"), qr("ขอลองเองก่อน", "ci_contact", "no")])


def confirm_edit(week, c):
    return text(f"สัปดาห์ที่ {week} ส่งไปแล้ว ({PROGRESS[c['progress']]}, มั่นใจ {c['confidence']}/5) "
                "ต้องการแก้ไขไหม?",
                [qr("✏️ แก้ไข", "ci_edit", "yes"), qr("ไม่ต้อง", "ci_edit", "no")])


def edit_kept():
    return text("โอเค คงข้อมูลเดิมไว้ ✅", _menu_quick())


def checkin_saved(week, progress, confidence, wants_contact, updated):
    head = f"อัปเดตแล้ว ✅ (สัปดาห์ที่ {week})" if updated else f"บันทึกแล้ว ✅ (สัปดาห์ที่ {week})"
    summary = f"{head}\nความคืบหน้า: {PROGRESS[progress]}\nความมั่นใจ: {confidence}/5"
    nxt = {
        "done": "เยี่ยมมาก 🎉 พักสักหน่อย แล้วรอรับงานสัปดาห์หน้านะ",
        "doing": "ดีแล้ว ไปต่อได้เลย ถ้าติดตรงไหนกดขอความช่วยเหลือได้เสมอ",
        "not_started": "ไม่เป็นไร เริ่มจากก้าวเล็กๆ ก่อนก็ได้ กดขอความช่วยเหลือได้ถ้าไม่รู้จะเริ่มตรงไหน",
        "stuck": "ขอบคุณที่บอกนะ การติดขัดเป็นเรื่องปกติ ทีมพร้อมช่วย",
    }[progress]
    if wants_contact:
        nxt += f"\n📞 ทีมจะติดต่อกลับ{SLA_TEXT}"
    return text(f"{summary}\n\n{nxt}", [qr("✏️ แก้ไข", "idle", "checkin_edit")] + _menu_quick())


# help
def ask_help_cat():
    return text("ต้องการความช่วยเหลือเรื่องอะไร?", [qr(v, "help_cat", k) for k, v in HELP_CATS.items()])


def ask_help_detail():
    return text("เล่าสั้นๆ ได้ไหม? พิมพ์ได้เลย หรือกดข้ามก็ได้", [qr("ข้าม", "help_detail", "skip")])


def help_created(help_id):
    return text(f"ส่งเรื่องแล้ว ✅ เลขเรื่อง #{help_id}\nสถานะ: {HELP_STATUS['waiting']}\n"
                f"ทีมจะตอบ{SLA_TEXT} ดูสถานะได้ที่ 'สถานะของฉัน'", _menu_quick())


def help_appended(help_id, status):
    return text(f"คุณมีเรื่อง #{help_id} ที่ยังเปิดอยู่ (สถานะ: {HELP_STATUS[status]}) "
                "เพิ่มข้อมูลเข้าเรื่องเดิมให้แล้ว ไม่ต้องส่งซ้ำนะ 🙂", _menu_quick())


def help_status_changed(help_id, status):
    return text(f"อัปเดตเรื่อง #{help_id}: {HELP_STATUS[status]}")


# status / info / resume / fallback
def status(week, checkin, helps, task=None):
    lines = [f"สถานะของคุณ (สัปดาห์ที่ {week})"]
    if task:
        lines.append(f"• งานสัปดาห์นี้: {task['title']}")
    if checkin:
        lines.append(f"• รายงานสัปดาห์นี้: ส่งแล้ว ({PROGRESS[checkin['progress']]}, มั่นใจ {checkin['confidence']}/5)")
    else:
        lines.append("• รายงานสัปดาห์นี้: ยังไม่ได้ส่ง")
    if helps:
        for h in helps:
            lines.append(f"• เรื่อง #{h['id']} ({HELP_CATS[h['category']]}): {HELP_STATUS[h['status']]}")
    else:
        lines.append("• เรื่องที่ขอความช่วยเหลือ: ไม่มี")
    return text("\n".join(lines), _menu_quick())


def info():
    return text("วิธีใช้ 📖\n• 'รายงานความคืบหน้า' ตอบสั้นๆ 3-4 คำถาม ใช้เวลาไม่ถึงนาที\n"
                "• 'ขอความช่วยเหลือ' ส่งเรื่องหาทีม พิมพ์ 'ช่วยด้วย' ได้ทุกเมื่อ\n"
                "• 'สถานะของฉัน' ดูว่าส่งแล้วหรือยัง\n\n"
                f"ทีมงานเป็นคนจริงๆ ตอบกลับ{SLA_TEXT} (วันทำการ)")


def resume(flow):
    name = "การรายงานความคืบหน้า" if flow == "checkin" else "การขอความช่วยเหลือ"
    return text(f"{name}ที่ค้างอยู่ ต่อจากเดิมเลยไหม?",
                [qr("ต่อจากเดิม", "resume", "continue"), qr("เริ่มใหม่", "resume", "restart")])


def unknown():
    return text("ขอโทษนะ ยังไม่เข้าใจ 🙏 เลือกจากปุ่มด้านล่างได้เลย")


def unknown_idle():
    return text("ขอโทษนะ ยังไม่เข้าใจ 🙏 เลือกจากเมนูด้านล่าง หรือกดปุ่มข้างล่างนี้ได้เลย", _menu_quick())


def stale():
    return text("ปุ่มนี้หมดอายุแล้ว ลองตอบจากคำถามล่าสุดนี้แทนนะ 👇")


def text_too_long():
    return text("ข้อความยาวไปหน่อย ช่วยย่อให้สั้นลง (ไม่เกิน 300 ตัวอักษร) ได้ไหม?")


# scheduled pushes
def weekly_start(week, task=None):
    return text(f"สัปดาห์ที่ {week} เริ่มแล้ว 🚀\n{task_line(week, task)}พร้อมเมื่อไหร่ กดรายงานความคืบหน้าได้เลย",
                _menu_quick())


def nudge(week, task=None):
    name = f" (งาน: {task['title']})" if task else ""
    return text(f"สัปดาห์ที่ {week} ผ่านมาครึ่งทางแล้ว{name} ลองรายงานความคืบหน้าสั้นๆ ไหม? ใช้เวลาไม่ถึงนาที 🙂",
                _menu_quick())


def ask_followup():
    return text("ช่วงที่ผ่านมา ได้ลองนำสิ่งที่ทำไปใช้จริงไหม? (เช่น ส่งสมัครงาน ใช้ Resume)",
                [qr(v, "fu", k) for k, v in FOLLOWUP.items()])


def followup_reply(outcome):
    return text({"used": "ยอดเยี่ยม 🎉 ขอบคุณที่บอกนะ",
                 "not_yet": "ไม่เป็นไรเลย ค่อยๆ ไปได้ ถ้าอยากให้ช่วย กดขอความช่วยเหลือได้",
                 "stuck": f"ขอบคุณที่บอกนะ เราส่งเรื่องให้ทีมแล้ว ทีมจะติดต่อกลับ{SLA_TEXT}"}[outcome],
                _menu_quick())
