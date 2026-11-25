import io
p=r"D:\capital lion\xueban\xueban-backend\xueban\api\xueban_teacher.py"
t=io.open(p,encoding="utf-8").read()
anchor='@router.post("/grade/jobs/{job_id}/pause")'
add=r'''@router.delete("/grade/jobs/{job_id}")
def delete_job(job_id: str, user: Dict[str, Any] = Depends(current_user)) -> Dict[str, Any]:
    require_teacher(user); init()
    job = db.q1("SELECT * FROM teacher_grade_jobs WHERE id=?", (job_id,))
    if not job:
        raise HTTPException(404, "判题任务不存在")
    if job.get("status") in ("running", "queued", "pausing"):
        raise HTTPException(409, "任务正在运行，请先暂停后再删除")
    items = db.q("SELECT answer_id, grade_id FROM teacher_grade_job_items WHERE job_id=?", (job_id,))
    grade_ids = [item["grade_id"] for item in items if item.get("grade_id")]
    affected = sorted({item["answer_id"] for item in items})
    with db.transaction():
        if grade_ids:
            placeholders = ",".join("?" for _ in grade_ids)
            db.x(f"DELETE FROM teacher_corrections WHERE grade_run_id IN ({placeholders})", tuple(grade_ids))
            db.x(f"DELETE FROM teacher_grade_runs WHERE id IN ({placeholders})", tuple(grade_ids))
        db.x("DELETE FROM teacher_grade_job_items WHERE job_id=?", (job_id,))
        db.x("DELETE FROM teacher_grade_jobs WHERE id=?", (job_id,))
        for answer_id in affected:
            grade = effective_grade(answer_id)
            if not grade:
                state = "pending"
            elif grade.get("status") == "failed":
                state = "failed"
            elif grade.get("needs_review"):
                state = "needs_review"
            else:
                state = "graded"
            db.x("UPDATE teacher_answers SET grading_status=? WHERE id=?", (state, answer_id))
    return {"deleted": True, "job_id": job_id, "answers_reset": len(affected), "grades_deleted": len(grade_ids)}

''' + anchor
assert anchor in t
t=t.replace(anchor,add,1)
io.open(p,"w",encoding="utf-8",newline="").write(t)
print('delete endpoint')