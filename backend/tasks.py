"""Weekly skill-development topics. Set by the team (one list shared by every learner goal).

DEFAULT_TASKS are placeholders so a fresh database works; the team replaces them on /board."""
WEEKS = (1, 2, 3, 4)  # main activity weeks; weeks 5-6 are follow-up
MAX_TITLE = 120
MAX_DESC = 500

DEFAULT_TASKS = {
    1: ("สำรวจตัวเอง", "เลือกสายงานที่สนใจ 1 อย่าง และเขียนทักษะที่มีอยู่ 5 ข้อ"),
    2: ("ลงมือสร้างชิ้นงานแรก", "ร่าง Resume หรือชิ้นงานตัวอย่าง 1 ชิ้น ยังไม่ต้องสมบูรณ์"),
    3: ("ขอ feedback แล้วปรับ", "ให้เพื่อนหรือรุ่นพี่อ่านชิ้นงาน แล้วแก้ 1-2 จุด"),
    4: ("นำไปใช้จริง", "ส่งสมัครงาน/ฝึกงาน 1 ที่ หรือซ้อมสัมภาษณ์ 1 ครั้ง"),
}


def validate_task(week, title, description):
    """Returns an error code, or None when valid."""
    if week not in WEEKS:
        return "week_must_be_1_to_4"
    if not isinstance(title, str) or not title.strip():
        return "title_required"
    if len(title.strip()) > MAX_TITLE:
        return "title_too_long"
    if description is not None and not isinstance(description, str):
        return "description_must_be_text"
    if len(description or "") > MAX_DESC:
        return "description_too_long"
    return None
