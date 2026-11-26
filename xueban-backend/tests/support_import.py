"""构造真实上传对象与 CSV 内容，供导入路径测试复用。

这些 helper 只生成内存中的 CSV 字节流，不写任何固定测试数据文件。
"""
from __future__ import annotations

import csv
import io
from typing import Any, Dict, Iterable, List

STUDENT_FIELDS = ["学生编号", "模拟班级编号", "年级"]
REQUIREMENT_FIELDS = ["主题编号", "主题", "主题目标", "指标编号", "观察指标", "课标整理编号"]
QUESTION_FIELDS = ["材料编号", "主题编号", "材料类型", "题型", "题干或内容", "参考答案", "参考解析", "满分", "评分点", "指标编号", "来源或资料文件"]
EXAM_FIELDS = ["节点编号", "模拟测验日期", "已教及测量主题", "可用题目编号", "说明"]
ANSWER_FIELDS = ["记录编号", "学生编号", "材料编号", "节点编号", "作答事件编号", "修订版本", "模拟提交时间", "评分状态", "得分", "满分", "学生最终作答", "解答过程"]


class FakeUpload:
    """最小 UploadFile 替身：read_csvs 只用到 filename 与 file.read()。"""

    def __init__(self, filename: str, content: str):
        self.filename = filename
        self.file = io.BytesIO(content.encode("utf-8-sig"))


def to_csv(rows: Iterable[Dict[str, Any]], fields: List[str]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({key: row.get(key, "") for key in fields})
    return buffer.getvalue()


def student(code: str = "S01", class_code: str = "C1", grade: str = "7") -> Dict[str, str]:
    return {"学生编号": code, "模拟班级编号": class_code, "年级": grade}


def requirement(topic: str = "T01", indicator: str = "I01") -> Dict[str, str]:
    return {"主题编号": topic, "主题": f"主题{topic}", "主题目标": "目标", "指标编号": indicator, "观察指标": f"知识点{indicator}", "课标整理编号": "ST1"}


def question(
    qid: str = "Q001",
    topic: str = "T01",
    indicator: str = "I01",
    stem: str = "题干",
    max_score: str = "5",
    scoring_points: str = "Q001P1 5分",
    reference: str = "参考答案",
    qtype: str = "解答题",
    material_type: str = "题目",
) -> Dict[str, str]:
    return {
        "材料编号": qid, "主题编号": topic, "材料类型": material_type, "题型": qtype,
        "题干或内容": stem, "参考答案": reference, "参考解析": "参考解析",
        "满分": max_score, "评分点": scoring_points, "指标编号": indicator, "来源或资料文件": "fixture.csv",
    }


def exam(node: str = "N01") -> Dict[str, str]:
    return {"节点编号": node, "模拟测验日期": "2026-01-20", "已教及测量主题": "T01", "可用题目编号": "Q001", "说明": "测验"}


def answer(
    event: str = "E1",
    student_code: str = "S01",
    qid: str = "Q001",
    node: str = "N01",
    revision: int = 1,
    final: str = "答案",
    process: str = "第一步\n第二步",
    max_score: str = "5",
    score: str = "0",
    status: str = "已评分",
) -> Dict[str, str]:
    return {
        "记录编号": f"R-{event}-{revision}", "学生编号": student_code, "材料编号": qid,
        "节点编号": node, "作答事件编号": event, "修订版本": str(revision),
        "模拟提交时间": "2026-01-20 10:00:00", "评分状态": status, "得分": score,
        "满分": max_score, "学生最终作答": final, "解答过程": process,
    }


def make_files(
    answers: List[Dict[str, str]],
    questions: List[Dict[str, str]] | None = None,
    students: List[Dict[str, str]] | None = None,
    requirements: List[Dict[str, str]] | None = None,
    exams: List[Dict[str, str]] | None = None,
) -> List[FakeUpload]:
    return [
        FakeUpload("模拟学生.csv", to_csv(students or [student()], STUDENT_FIELDS)),
        FakeUpload("课程要求.csv", to_csv(requirements or [requirement()], REQUIREMENT_FIELDS)),
        FakeUpload("题目与材料.csv", to_csv(questions or [question()], QUESTION_FIELDS)),
        FakeUpload("测验节点.csv", to_csv(exams or [exam()], EXAM_FIELDS)),
        FakeUpload("作答记录.csv", to_csv(answers, ANSWER_FIELDS)),
    ]
