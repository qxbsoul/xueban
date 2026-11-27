"""v1.5.21 HTTP reliability: local mock HTTP server + real SQLite grading records."""
import json
import os
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

os.environ["DEEPSEEK_API_KEY"] = "test-key"
os.environ["XUEBAN_HTTP_CONNECT_TIMEOUT"] = "2"
os.environ["XUEBAN_HTTP_READ_TIMEOUT"] = "1"
os.environ["XUEBAN_HTTP_TOTAL_TIMEOUT"] = "6"
os.environ["XUEBAN_GRADER_ATTEMPTS"] = "3"
os.environ["XUEBAN_GRADER_RETRY_BASE_MS"] = "1"

from xueban.api import xueban_teacher as t
from xueban.services import xueban_store as db

NOW = "2026-07-01T00:00:00"
B = "batch_http1521"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        script = self.server.script
        mode = script.pop(0) if script else "success"
        if mode == "slow":
            time.sleep(2)
        if mode == "incomplete":
            self.send_response(200)
            self.send_header("Content-Length", "1000")
            self.end_headers()
            self.wfile.write(b'{"choices":')
            self.wfile.flush()
            self.close_connection = True
            return
        if mode == "429":
            self.send_response(429)
            self.send_header("Retry-After", "0")
            self.end_headers()
            self.wfile.write(b"limited")
            return
        if mode == "500":
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"server")
            return
        if mode == "bad_envelope":
            self._json({"error": "no choices"})
            return
        content = json.dumps({
            "score": 2, "max_score": 2, "is_correct": "correct",
            "step_results": [{"step_index": 1, "source_step_id": "src-1", "student_step": "1+1=2", "is_correct": True, "score": 2, "max_score": 2, "error_type": "", "judgment_reason": "正确", "scoring_point_links": [{"scoring_point_id": "Q001P1", "awarded_score": 2, "max_score": 2}], "operation_units": [{"operation_id": "op-1", "source_step_id": "src-1", "student_fragment": "1+1=2", "is_correct": True, "score": 2, "max_score": 2, "error_type": "", "judgment_reason": "正确", "scoring_point_links": [{"scoring_point_id": "Q001P1", "awarded_score": 2, "max_score": 2}]}]}],
        }, ensure_ascii=False)
        if mode == "truncated_content":
            content = '{"score":1'
        if mode == "bad_scoring":
            content = json.dumps({"score": 2, "max_score": 2, "is_correct": "correct", "step_results": [{"step_index": 1, "source_step_id": "src-1", "student_step": "1+1=2", "is_correct": True, "score": 2, "max_score": 2, "error_type": "", "judgment_reason": "正确", "scoring_point_links": [{"scoring_point_id": "Q001P1", "awarded_score": 4, "max_score": 4}], "operation_units": [{"operation_id": "op-1", "source_step_id": "src-1", "student_fragment": "1+1=2", "is_correct": True, "score": 2, "max_score": 2, "error_type": "", "judgment_reason": "正确", "scoring_point_links": [{"scoring_point_id": "Q001P1", "awarded_score": 4, "max_score": 4}]}]}]}, ensure_ascii=False)
        if mode == "null_score":
            content = json.dumps({"score": None, "max_score": 1, "is_correct": "correct", "step_results": []}, ensure_ascii=False)
        self._json({"choices": [{"message": {"content": content}}]})

    def _json(self, payload):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
server.script = []
threading.Thread(target=server.serve_forever, daemon=True).start()
os.environ["XUEBAN_LLM_BASE_URL"] = f"http://127.0.0.1:{server.server_port}"

t.init()
db.x("DELETE FROM teacher_answers WHERE import_batch_id=?", (B,))
db.x("DELETE FROM teacher_batches WHERE id=?", (B,))
db.x("INSERT INTO teacher_batches (id,filename,status,counts_json,errors_json,preprocess_status,created_at) VALUES (?,?,?,?,?,?,?)", (B, "http.csv", "committed", "{}", "[]", "ready", NOW))
db.x("INSERT INTO teacher_classes (id,code,name,grade,created_at) VALUES (?,?,?,?,?)", ("class_http", "HC", "HTTP班", 7, NOW))
db.x("INSERT INTO teacher_students (id,user_id,code,class_id,name,grade,created_at) VALUES (?,?,?,?,?,?,?)", ("stu_http", "u_http", "H01", "class_http", "H01", 7, NOW))
db.x("INSERT INTO teacher_questions (id,stem,reference_answer,max_score,scoring_points,question_type) VALUES (?,?,?,?,?,?)", ("Q001", "1+1", "2", 2, "Q001P1 2分", "解答题"))
db.x("INSERT INTO teacher_question_versions (version_id,batch_id,question_id,stem,reference_answer,max_score,scoring_points,question_type,created_at) VALUES (?,?,?,?,?,?,?,?,?)", ("qv:Q001", B, "Q001", "1+1", "2", 2, "Q001P1 2分", "解答题", NOW))


def make_answer(suffix: str) -> str:
    answer_id = f"ans_{suffix}"
    db.x("INSERT OR REPLACE INTO teacher_answers (id,student_id,question_id,event_id,revision,grading_status,import_batch_id,max_score,original_answer,solution_process,question_version_id,exam_node_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
         (answer_id, "stu_http", "Q001", f"E-{suffix}", 1, "pending", B, 2, "2", "1+1=2", "qv:Q001", "N01"))
    return answer_id


def make_job(job_id: str, answer_id: str, concurrency: int = 1) -> None:
    db.x("INSERT OR REPLACE INTO teacher_grade_jobs (id,status,batch_id,total,processed,completed,failed,needs_review,concurrency,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)", (job_id, "queued", B, 1, 0, 0, 0, 0, concurrency, NOW, NOW))
    db.x("INSERT OR REPLACE INTO teacher_grade_job_items (id,job_id,answer_id,status,attempt_count,updated_at) VALUES (?,?,?,?,?,?)", (f"item_{job_id}", job_id, answer_id, "pending", 0, NOW))


def run_scenario(name: str, script: list, caller=None) -> dict:
    answer_id = make_answer(name)
    job_id = f"job_{name}"
    make_job(job_id, answer_id)
    server.script = list(script)
    previous = t._LLM_CALLER
    t._LLM_CALLER = caller or (lambda answer, question, steps, key, provider, standard, context: t.llm_grade(answer, question, steps, key, provider, standard, context))
    try:
        t.run_job(job_id)
    finally:
        t._LLM_CALLER = previous
    item = dict(db.q1("SELECT * FROM teacher_grade_job_items WHERE job_id=?", (job_id,)))
    runs = db.q("SELECT id,status,error_type FROM teacher_grade_runs WHERE answer_id=? ORDER BY grading_version", (answer_id,))
    attempts = db.q("SELECT error_type,retryable FROM teacher_grade_attempts WHERE answer_id=? ORDER BY rowid", (answer_id,))
    return {"status": item.get("status"), "attempt_count": item.get("attempt_count"), "last_error_type": item.get("last_error_type"), "result_id": item.get("result_id"), "runs": runs, "attempts": attempts}


results = {}
for name, script in (("success", ["success"]), ("read_timeout", ["slow", "success"]), ("incomplete", ["incomplete", "success"]), ("rate_limit", ["429", "success"]), ("server_500", ["500", "success"])):
    results[name] = run_scenario(name, script)
results["incomplete_json"] = run_scenario("incomplete_json", ["truncated_content", "truncated_content"])
results["bad_scoring"] = run_scenario("bad_scoring", ["bad_scoring", "bad_scoring"])
results["null_score"] = run_scenario("null_score", ["null_score", "null_score"])
results["bad_envelope"] = run_scenario("bad_envelope", ["bad_envelope"])

# SSL 断连后恢复：第一次抛 SSLEOFError，第二次返回合法结构
valid = {"score": 2, "max_score": 2, "is_correct": "correct", "step_results": [{"step_index": 1, "source_step_id": "src-1", "student_step": "1+1=2", "is_correct": True, "score": 2, "max_score": 2, "error_type": "", "judgment_reason": "正确", "scoring_point_links": [{"scoring_point_id": "Q001P1", "awarded_score": 2, "max_score": 2}], "operation_units": [{"operation_id": "op-1", "source_step_id": "src-1", "student_fragment": "1+1=2", "is_correct": True, "score": 2, "max_score": 2, "error_type": "", "judgment_reason": "正确", "scoring_point_links": [{"scoring_point_id": "Q001P1", "awarded_score": 2, "max_score": 2}]}]}]}
calls = {"n": 0}


def ssl_caller(*args):
    calls["n"] += 1
    if calls["n"] == 1:
        raise ssl.SSLEOFError("unexpected eof")
    return dict(valid)


results["ssl_recover"] = run_scenario("ssl_recover", [], caller=ssl_caller)

print(json.dumps({"results": results, "calls": calls["n"]}, ensure_ascii=False, default=str))
server.shutdown()
