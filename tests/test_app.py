"""关键操作自测：上传校验、键计算、判定/复核、停供恢复、回滚、不重扫原表、会话竞态。"""
import io
import json
import sqlite3
import threading
import time

import pytest

from app import services
from app.db import get_db
from app.keyfind import find_min_keys
from app.main import create_app


@pytest.fixture()
def client(tmp_path):
    app = create_app(
        {
            "DATABASE": str(tmp_path / "t.db"),
            "SECRET_KEY": "t",
            "TESTING": True,
            "PROPAGATE_EXCEPTIONS": False,  # 与线上一致：异常返回 500 而非抛出
        }
    )
    return app.test_client()


# ---------------------------------------------------------------- 工具

def csv_bytes(header, rows):
    lines = [",".join(header)] + [",".join(map(str, r)) for r in rows]
    return "\n".join(lines).encode("utf-8")


def upload(client, a, b):
    return client.post(
        "/api/upload",
        data={
            "file_a": (io.BytesIO(a), "a.csv"),
            "file_b": (io.BytesIO(b), "b.csv"),
        },
        content_type="multipart/form-data",
    )


def sid(client):
    return client.get("/api/state").get_json()["session_id"]


def make_pair():
    """两表均有单列键 idc、bc；note 含空值（应被排除）。

    冲突对：旧表第2行 P2/T2，新表第2行 P2/T9 —— idc 相同、bc 不同。
    """
    header = ["idc", "bc", "note"]
    a = [["P1", "T1", "x"], ["P2", "T2", "x"], ["P3", "T3", ""]]
    b = [["P1", "T1", "x"], ["P2", "T9", "x"], ["P4", "T4", "x"]]
    return csv_bytes(header, a), csv_bytes(header, b)


def setup_pair(client):
    a, b = make_pair()
    assert upload(client, a, b).status_code == 200
    s = sid(client)
    assert client.post(
        "/api/columns", json={"columns": ["idc", "bc", "note"], "session_id": s}
    ).status_code == 200
    keys = client.post(
        "/api/keys/compute", json={"session_id": s}
    ).get_json()["keys"]
    return {tuple(k["cols"]): k["id"] for k in keys}


def state(client):
    return client.get("/api/state").get_json()


# ---------------------------------------------------------------- 上传

def test_upload_ok(client):
    a, b = make_pair()
    r = upload(client, a, b)
    assert r.status_code == 200
    body = r.get_json()
    assert body["rows_a"] == 3 and body["rows_b"] == 3
    assert body["columns"] == ["idc", "bc", "note"]


def test_upload_header_mismatch(client):
    a = csv_bytes(["x", "y"], [["1", "2"]])
    b = csv_bytes(["x", "z"], [["1", "2"]])
    r = upload(client, a, b)
    assert r.status_code == 400
    assert "表头不一致" in r.get_json()["error"]


def test_upload_row_limit(client):
    header = ["u"]
    a = csv_bytes(header, [[f"a{i}"] for i in range(20001)])
    b = csv_bytes(header, [["b1"]])
    r = upload(client, a, b)
    assert r.status_code == 400
    assert "20000" in r.get_json()["error"]


def test_upload_bad_row_width(client):
    a = csv_bytes(["x", "y"], [["1", "2"], ["3"]])
    b = csv_bytes(["x", "y"], [["1", "2"]])
    assert upload(client, a, b).status_code == 400


# ---------------------------------------------------------------- 备选列

def test_columns_validation(client):
    a, b = make_pair()
    upload(client, a, b)
    s = sid(client)
    assert client.post("/api/columns", json={"columns": [], "session_id": s}).status_code == 400
    assert (
        client.post(
            "/api/columns",
            json={"columns": [f"c{i}" for i in range(11)], "session_id": s},
        ).status_code
        == 400
    )
    assert client.post(
        "/api/columns", json={"columns": ["idc", "idc"], "session_id": s}
    ).status_code == 400
    assert client.post(
        "/api/columns", json={"columns": ["nope"], "session_id": s}
    ).status_code == 400
    assert client.post(
        "/api/columns", json={"columns": ["idc", "bc"], "session_id": s}
    ).status_code == 200


# ---------------------------------------------------------------- 键计算

def test_find_min_keys_unit():
    rows_a = [{"u": "1", "e": "", "x": "p"}, {"u": "2", "e": "v", "x": "p"}]
    rows_b = [{"u": "a", "e": "v", "x": "q"}, {"u": "b", "e": "v", "x": "q"}]
    # e 含空值被排除；x 两表均重复；u 唯一 → 唯一的最小键
    assert find_min_keys(["u", "e", "x"], rows_a, rows_b) == [("u",)]


def test_compute_min_keys(client):
    keys = setup_pair(client)
    assert set(keys) == {("idc",), ("bc",)}  # note 含空值被排除


def test_compute_size2_when_no_single(client):
    header = ["x", "y"]
    a = csv_bytes(header, [["p", "1"], ["p", "2"], ["q", "1"]])
    b = csv_bytes(header, [["m", "1"], ["m", "2"], ["n", "1"]])
    assert upload(client, a, b).status_code == 200
    s = sid(client)
    client.post("/api/columns", json={"columns": ["x", "y"], "session_id": s})
    keys = client.post("/api/keys/compute", json={"session_id": s}).get_json()["keys"]
    assert [k["cols"] for k in keys] == [["x", "y"]]


def test_compute_no_valid_key(client):
    header = ["x"]
    a = csv_bytes(header, [["p"], ["p"]])
    b = csv_bytes(header, [["m"]])
    upload(client, a, b)
    s = sid(client)
    client.post("/api/columns", json={"columns": ["x"], "session_id": s})
    keys = client.post("/api/keys/compute", json={"session_id": s}).get_json()["keys"]
    assert keys == []


# ---------------------------------------------------------------- 选键与下载

def test_download_requires_serving(client):
    setup_pair(client)
    assert client.get(f"/download.csv?session_id={sid(client)}").status_code == 409


def test_select_and_download(client):
    keys = setup_pair(client)
    s = sid(client)
    assert client.post(
        "/api/keys/select", json={"key_id": keys[("idc",)], "session_id": s}
    ).status_code == 200
    r = client.get(f"/download.csv?session_id={s}")
    assert r.status_code == 200
    assert "attachment" in r.headers["Content-Disposition"]
    body = r.get_data(as_text=True)
    assert "P1,1,1,配对" in body
    assert "P2,2,2,配对" in body
    assert "P3,3,,仅旧表" in body
    assert "P4,,3,仅新表" in body


def test_select_incompatible_key_rejected(client):
    keys = setup_pair(client)
    s = sid(client)
    client.post("/api/keys/select", json={"key_id": keys[("idc",)], "session_id": s})
    # 异次判定使 idc 键失效
    client.post(
        "/api/judgments",
        json={"row_a": 2, "row_b": 2, "type": "diff", "session_id": s},
    )
    r = client.post(
        "/api/keys/select", json={"key_id": keys[("idc",)], "session_id": s}
    )
    assert r.status_code == 409


# ---------------------------------------------------------------- 判定 / 复核

def test_judgment_flow_full(client):
    keys = setup_pair(client)
    s = sid(client)
    client.post("/api/keys/select", json={"key_id": keys[("idc",)], "session_id": s})

    # 同次判定（两键值均相同）：不失效，继续供数
    r = client.post(
        "/api/judgments",
        json={"row_a": 1, "row_b": 1, "type": "same", "session_id": s},
    ).get_json()
    assert r["serving"] and r["invalidated"] == []
    assert client.get(f"/download.csv?session_id={s}").status_code == 200

    # 异次判定落在 idc 相同的行对上：idc 键失效并停供
    r = client.post(
        "/api/judgments",
        json={"row_a": 2, "row_b": 2, "type": "diff", "session_id": s},
    ).get_json()
    assert not r["serving"] and r["invalidated"] == [keys[("idc",)]]
    assert client.get(f"/download.csv?session_id={s}").status_code == 409

    st = state(client)
    k = {tuple(x["cols"]): x["compatible"] for x in st["keys"]}
    assert k == {("idc",): False, ("bc",): True}

    # 另选 bc 键恢复供数；冲突对按判定拆成两条单边
    client.post("/api/keys/select", json={"key_id": keys[("bc",)], "session_id": s})
    body = client.get(f"/download.csv?session_id={s}").get_data(as_text=True)
    assert "T2,2,,仅旧表" in body and "T9,,2,仅新表" in body

    # 撤销异次判定：idc 恢复合规；在用 bc 键仍合规 → 继续供数
    diff_id = next(j["id"] for j in state(client)["judgments"] if j["typ"] == "diff")
    r = client.post(
        "/api/judgments/revoke", json={"id": diff_id, "session_id": s}
    ).get_json()
    assert keys[("idc",)] in r["restored"]
    st = state(client)
    assert st["serving"] is True
    assert client.get(f"/download.csv?session_id={s}").status_code == 200

    # 重新选用 idc 键亦可
    client.post("/api/keys/select", json={"key_id": keys[("idc",)], "session_id": s})
    assert client.get(f"/download.csv?session_id={s}").status_code == 200


def test_no_auto_resume_after_key_restored(client):
    """在用键失效停供后，即使撤销判定使其恢复合规，也须重新选用才恢复下载。"""
    keys = setup_pair(client)
    s = sid(client)
    client.post("/api/keys/select", json={"key_id": keys[("idc",)], "session_id": s})
    client.post(
        "/api/judgments",
        json={"row_a": 2, "row_b": 2, "type": "diff", "session_id": s},
    )
    assert state(client)["serving"] is False

    diff_id = state(client)["judgments"][0]["id"]
    client.post("/api/judgments/revoke", json={"id": diff_id, "session_id": s})
    st = state(client)
    k = {tuple(x["cols"]): x["compatible"] for x in st["keys"]}
    assert k[("idc",)] is True            # 键已恢复合规
    assert st["serving"] is False          # 但供数不自动恢复
    assert client.get(f"/download.csv?session_id={s}").status_code == 409

    client.post("/api/keys/select", json={"key_id": keys[("idc",)], "session_id": s})
    assert client.get(f"/download.csv?session_id={s}").status_code == 200


def test_judgment_validation(client):
    setup_pair(client)
    s = sid(client)
    assert (
        client.post(
            "/api/judgments",
            json={"row_a": 99, "row_b": 1, "type": "same", "session_id": s},
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/judgments",
            json={"row_a": 1, "row_b": 1, "type": "bad", "session_id": s},
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/judgments",
            json={"row_a": 1, "row_b": 1, "type": "same", "session_id": s},
        ).status_code
        == 200
    )
    # 同一对行重复登记（含异次冲突）须先撤销
    assert (
        client.post(
            "/api/judgments",
            json={"row_a": 1, "row_b": 1, "type": "same", "session_id": s},
        ).status_code
        == 409
    )
    assert (
        client.post(
            "/api/judgments",
            json={"row_a": 1, "row_b": 1, "type": "diff", "session_id": s},
        ).status_code
        == 409
    )


def test_revoke_unknown_404(client):
    setup_pair(client)
    s = sid(client)
    assert client.post(
        "/api/judgments/revoke", json={"id": 42, "session_id": s}
    ).status_code == 404


# ---------------------------------------------------------------- 事务回滚

def test_save_failure_rolls_back(client, monkeypatch):
    keys = setup_pair(client)
    s = sid(client)
    client.post("/api/keys/select", json={"key_id": keys[("idc",)], "session_id": s})
    client.post(
        "/api/judgments",
        json={"row_a": 1, "row_b": 1, "type": "same", "session_id": s},
    )

    def boom(conn):
        raise RuntimeError("模拟保存失败")

    monkeypatch.setattr(services, "refresh_candidates", boom)
    r = client.post(
        "/api/judgments",
        json={"row_a": 2, "row_b": 2, "type": "diff", "session_id": s},
    )
    assert r.status_code == 500

    # 原判定与供数状态保持不变
    st = state(client)
    assert len(st["judgments"]) == 1
    assert st["judgments"][0]["typ"] == "same"
    assert st["serving"] is True and st["can_download"] is True
    assert client.get(f"/download.csv?session_id={s}").status_code == 200


# ---------------------------------------------------------------- 不重扫原表

def test_refresh_without_rescanning_sources(client, tmp_path):
    keys = setup_pair(client)
    s = sid(client)
    client.post("/api/keys/select", json={"key_id": keys[("idc",)], "session_id": s})

    # 直接清空两张原表：复核更新只能依赖键值投影
    db_path = None
    with client.application.app_context():
        db_path = client.application.config["DATABASE"]
    conn = sqlite3.connect(db_path)
    conn.execute("DELETE FROM rows_a")
    conn.execute("DELETE FROM rows_b")
    conn.commit()
    conn.close()

    # 登记/撤销判定与候选更新照常工作（行号上限来自 meta，不读原表）
    r = client.post(
        "/api/judgments",
        json={"row_a": 2, "row_b": 2, "type": "diff", "session_id": s},
    )
    assert r.status_code == 200
    assert r.get_json()["invalidated"] == [keys[("idc",)]]
    st = state(client)
    k = {tuple(x["cols"]): x["compatible"] for x in st["keys"]}
    assert k == {("idc",): False, ("bc",): True}

    jid = st["judgments"][0]["id"]
    r = client.post(
        "/api/judgments/revoke", json={"id": jid, "session_id": s}
    )
    assert r.status_code == 200
    k = {tuple(x["cols"]): x["compatible"] for x in state(client)["keys"]}
    assert k == {("idc",): True, ("bc",): True}


# ---------------------------------------------------------------- 会话重置

def test_upload_resets_session(client):
    keys = setup_pair(client)
    s = sid(client)
    client.post("/api/keys/select", json={"key_id": keys[("idc",)], "session_id": s})
    client.post(
        "/api/judgments",
        json={"row_a": 1, "row_b": 1, "type": "same", "session_id": s},
    )
    a, b = make_pair()
    r = upload(client, a, b)
    assert r.status_code == 200
    new_s = r.get_json()["session_id"]
    assert new_s and new_s != s  # 每次上传换发新会话标识
    st = state(client)
    assert st["session_id"] == new_s
    assert st["keys"] == [] and st["judgments"] == []
    assert st["serving"] is False and st["current_key_id"] is None


# ---------------------------------------------------- 会话过期 / 竞态防护

MUTATING_PATHS = [
    ("POST", "/api/columns", {"columns": ["idc"]}),
    ("POST", "/api/keys/compute", {}),
    ("POST", "/api/keys/select", {"key_id": 1}),
    ("POST", "/api/judgments", {"row_a": 1, "row_b": 1, "type": "same"}),
    ("POST", "/api/judgments/revoke", {"id": 1}),
]


def test_stale_session_rejected_after_reupload(client):
    """重新上传后，携带旧 session_id 的一切写操作一律 409 会话过期。"""
    setup_pair(client)
    old_s = sid(client)
    a, b = make_pair()
    upload(client, a, b)
    assert sid(client) != old_s

    for method, path, payload in MUTATING_PATHS:
        r = client.open(
            path, method=method, json={**payload, "session_id": old_s}
        )
        assert r.status_code == 409, (path, r.status_code)
        assert "会话已过期" in r.get_json()["error"], path

    # 旧会话下载同样被拒，且不产生 500
    r = client.get(f"/download.csv?session_id={old_s}")
    assert r.status_code == 409 and "会话已过期" in r.get_data(as_text=True)

    # 缺少会话标识 -> 400（而不是 500）
    r = client.post("/api/keys/compute", json={})
    assert r.status_code == 400 and "会话标识" in r.get_json()["error"]
    r = client.get("/download.csv")
    assert r.status_code == 400


def test_reuploaded_session_is_single_source_of_truth(client):
    """新会话的键、投影、表头、行数、供数状态必须对应同一份新台账。"""
    setup_pair(client)  # 旧台账 idc/bc/note
    new_header = ["code", "name"]
    # 新表 B 的 code 出现重复（C1 两次）-> code 不能成键；name 两表均唯一
    na = csv_bytes(new_header, [["C1", "n1"], ["C2", "n2"], ["C5", "n5"]])
    nb = csv_bytes(new_header, [["C1", "n1"], ["C3", "n3"], ["C1", "n9"]])
    r = upload(client, na, nb)
    s = r.get_json()["session_id"]

    st = state(client)
    assert st["header"] == new_header and st["n_a"] == 3 and st["n_b"] == 3
    assert st["keys"] == [] and st["judgments"] == []
    assert st["serving"] is False and st["current_key_id"] is None

    # 按新台账重算：code 含重复值不唯一，name 唯一 -> 仅 name 成键
    assert client.post(
        "/api/columns", json={"columns": new_header, "session_id": s}
    ).status_code == 200
    keys = client.post(
        "/api/keys/compute", json={"session_id": s}
    ).get_json()["keys"]
    assert [k["cols"] for k in keys] == [["name"]]

    # 投影只属于新键，且行数与新台账一致
    with client.application.app_context():
        db = get_db()
        key_id = keys[0]["id"]
        proj = db.execute(
            "SELECT ds, COUNT(*) c FROM projections WHERE key_id=? GROUP BY ds",
            (key_id,),
        ).fetchall()
        assert {r["ds"]: r["c"] for r in proj} == {"A": 3, "B": 3}
        assert db.execute("SELECT COUNT(*) c FROM keys").fetchone()["c"] == 1

    # 选键、下载全程不触发 500，输出键列来自新表头
    assert client.post(
        "/api/keys/select", json={"key_id": key_id, "session_id": s}
    ).status_code == 200
    r = client.get(f"/download.csv?session_id={s}")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert body.startswith("﻿name,旧表行号")
    assert "n1,1,1,配对" in body
    assert "n5,3,,仅旧表" in body
    assert "n3,,2,仅新表" in body


def test_compute_during_reupload_cannot_pollute_new_session(client, monkeypatch):
    """核心竞态：算键进行中重新上传，旧计算必须 409，新会话不留旧键/旧投影。"""
    setup_pair(client)
    old_s = sid(client)

    started = threading.Event()
    release = threading.Event()

    def slow_find(columns, rows_a, rows_b):
        started.set()
        release.wait(timeout=5)
        return find_min_keys(columns, rows_a, rows_b)

    monkeypatch.setattr(services, "find_min_keys", slow_find)

    holder = {}

    def compute():
        holder["resp"] = client.post(
            "/api/keys/compute", json={"session_id": old_s}
        )

    t = threading.Thread(target=compute)
    t.start()
    assert started.wait(timeout=5)  # 旧算键已进入耗时窗口

    # 另一请求完成重新上传（表头完全不同的新台账）
    new_header = ["code", "name"]
    # 新表 B 的 name 重复（n1 两次）-> 仅 code 是单列合规键
    na = csv_bytes(new_header, [["C1", "n1"], ["C2", "n2"]])
    nb = csv_bytes(new_header, [["C1", "n1"], ["C3", "n1"]])
    r = upload(client, na, nb)
    assert r.status_code == 200
    new_s = r.get_json()["session_id"]

    release.set()
    t.join(timeout=5)
    assert not t.is_alive()
    assert holder["resp"].status_code == 409
    assert "会话已过期" in holder["resp"].get_json()["error"]

    st = state(client)
    # 旧列名键/投影零残留
    assert st["session_id"] == new_s
    assert st["keys"] == []
    assert st["header"] == new_header
    with client.application.app_context():
        db = get_db()
        assert db.execute("SELECT COUNT(*) c FROM keys").fetchone()["c"] == 0
        assert db.execute("SELECT COUNT(*) c FROM projections").fetchone()["c"] == 0
        assert db.execute("SELECT COUNT(*) c FROM judgments").fetchone()["c"] == 0

    # 新会话按新台账重算并下载成功，全程无 500
    client.post("/api/columns", json={"columns": new_header, "session_id": new_s})
    keys = client.post(
        "/api/keys/compute", json={"session_id": new_s}
    ).get_json()["keys"]
    assert [k["cols"] for k in keys] == [["code"]]
    client.post(
        "/api/keys/select", json={"key_id": keys[0]["id"], "session_id": new_s}
    )
    assert client.get(f"/download.csv?session_id={new_s}").status_code == 200


def test_concurrent_compute_and_judgment_share_one_session(client, monkeypatch):
    """同一会话内算键与判定并发：判定在算键窗口内先提交，算键随后按最新状态处理，无 500。"""
    keys = setup_pair(client)
    s = sid(client)

    started = threading.Event()
    release = threading.Event()

    def gated_find(columns, rows_a, rows_b):
        started.set()
        release.wait(timeout=5)
        return find_min_keys(columns, rows_a, rows_b)

    monkeypatch.setattr(services, "find_min_keys", gated_find)
    results = []

    def compute():
        results.append(
            ("compute", client.post("/api/keys/compute", json={"session_id": s}).status_code)
        )

    t = threading.Thread(target=compute)
    t.start()
    assert started.wait(timeout=5)

    # 算键读快照期间，判定事务先完成（行2/2 异次 → idc 失效、bc 仍合规）
    r = client.post(
        "/api/judgments",
        json={"row_a": 2, "row_b": 2, "type": "diff", "session_id": s},
    )
    assert r.status_code == 200
    results.append(("judgment", r.status_code))

    release.set()
    t.join(timeout=10)
    assert not t.is_alive()
    assert all(code == 200 for _, code in results), results

    st = state(client)
    # 无论提交顺序如何，键与判定都属于同一份台账（列名仍在表头内），
    # 且算键在判定之后提交，候选合规状态已纳入该判定
    for k in st["keys"]:
        assert all(c in st["header"] for c in k["cols"])
    assert len(st["judgments"]) == 1
    compat = {tuple(k["cols"]): k["compatible"] for k in st["keys"]}
    assert compat == {("idc",): False, ("bc",): True}


def test_html_forms_reject_stale_session_with_flash(client):
    """网页表单携带旧 session_id 时回显过期提示，状态保持新会话不变。"""
    setup_pair(client)
    old_s = sid(client)
    a, b = make_pair()
    upload(client, a, b)

    r = client.post(
        "/keys/compute", data={"session_id": old_s}, follow_redirects=True
    )
    assert r.status_code == 200
    assert "会话已过期" in r.get_data(as_text=True)
    assert state(client)["keys"] == []

    r = client.post(
        "/keys/select", data={"session_id": old_s, "key_id": 1}, follow_redirects=True
    )
    assert r.status_code == 200
    assert "会话已过期" in r.get_data(as_text=True)
    assert state(client)["serving"] is False

    r = client.post(
        "/judgments",
        data={"session_id": old_s, "row_a": 1, "row_b": 1, "typ": "same"},
        follow_redirects=True,
    )
    assert r.status_code == 200
    assert "会话已过期" in r.get_data(as_text=True)
    assert state(client)["judgments"] == []

    # 新会话的页面表单渲染了新的隐藏标识，下载链接同样携带
    page = client.get("/").get_data(as_text=True)
    new_s = sid(client)
    assert f'name="session_id" value="{new_s}"' in page
    assert f"session_id={new_s}" in page or new_s in page


# ---------------------------------------------------------- 写锁排队 / 超时兜底

def test_concurrent_upload_waits_for_write_lock_instead_of_500(client, monkeypatch):
    """长写事务持锁期间，并发上传排队等锁，完成后成为唯一有效会话（不出现裸 500）。"""
    setup_pair(client)
    s = sid(client)
    entered = threading.Event()
    release = threading.Event()

    from app.db import transaction as real_transaction

    def slow_transaction(conn):
        entered.set()
        release.wait(timeout=5)
        return real_transaction(conn)

    # 让判定事务持锁不释放，模拟大批量算键的长写事务
    monkeypatch.setattr(services, "transaction", slow_transaction)
    holder = {}

    def judge():
        holder["judge"] = client.post(
            "/api/judgments",
            json={"row_a": 1, "row_b": 1, "type": "same", "session_id": s},
        )

    t = threading.Thread(target=judge)
    t.start()
    assert entered.wait(timeout=5)  # 判定已在事务内持锁

    # 上传在另一线程发起，应阻塞等锁而不是立即 database is locked 500
    a, b = make_pair()
    up = {}

    def do_upload():
        up["r"] = upload(client, a, b)

    tu = threading.Thread(target=do_upload)
    tu.start()
    time.sleep(1.0)
    assert tu.is_alive(), "上传未排队等锁（可能直接失败了）"

    release.set()
    t.join(timeout=10)
    tu.join(timeout=10)
    assert not t.is_alive() and not tu.is_alive()
    assert holder["judge"].status_code == 200
    assert up["r"].status_code == 200  # 排队拿到锁后成功，而非 500/503

    st = state(client)
    # 上传最后提交 -> 它的会话是唯一有效会话，旧判定被清空
    assert st["judgments"] == [] and st["keys"] == []
    assert st["session_id"] == up["r"].get_json()["session_id"]


def test_lock_timeout_returns_clean_503_not_raw_500(client, monkeypatch):
    """busy_timeout 耗尽仍拿不到写锁时，API 返回 503 明确提示，网页回显 flash。"""
    setup_pair(client)
    s = sid(client)

    def always_locked(conn):
        import sqlite3

        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(services, "transaction", always_locked)

    # JSON API：503 + 明确文案
    r = client.post(
        "/api/judgments",
        json={"row_a": 1, "row_b": 1, "type": "same", "session_id": s},
    )
    assert r.status_code == 503
    assert "稍后重试" in r.get_json()["error"]

    # 网页表单：回首页 flash 提示，不抛 500
    r = client.post(
        "/judgments",
        data={"session_id": s, "row_a": 1, "row_b": 1, "typ": "same"},
        follow_redirects=True,
    )
    assert r.status_code == 200
    assert "稍后重试" in r.get_data(as_text=True)
