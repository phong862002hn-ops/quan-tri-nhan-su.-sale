def grade_score(total_score: float, blacklist_triggered: bool) -> str:
    if blacklist_triggered:
        return "Không đạt"
    if total_score >= 90:
        return "Xuất sắc"
    if total_score >= 75:
        return "Tốt"
    if total_score >= 50:
        return "Trung bình"
    return "Không đạt"
