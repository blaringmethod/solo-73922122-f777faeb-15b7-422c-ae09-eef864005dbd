"""Flask 入口：网页路由（表单）+ JSON API（/api/...）。"""
import functools
import os
import sqlite3

from flask import (
    Flask,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    Response,
    url_for,
)

from . import services
from .db import close_db, get_db, init_db
from .services import ServiceError

MAX_CONTENT_LENGTH = 64 * 1024 * 1024  # 两份 2 万行 CSV 绰绰有余
LOCKED_MESSAGE = "数据库正忙于另一项长操作（如大批量算键），请稍后重试。"


def create_app(config=None):
    app = Flask(__name__)
    app.config.from_mapping(
        DATABASE=os.environ.get("DATABASE", os.path.join("data", "app.db")),
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-secret"),
        MAX_CONTENT_LENGTH=MAX_CONTENT_LENGTH,
    )
    if config:
        app.config.update(config)

    init_db(app.config["DATABASE"])
    app.teardown_appcontext(close_db)

    # ----------------------------------------------------------- 网页

    def html_view(fn):
        """网页表单统一异常处理：失败回显 flash，状态保持不变。"""

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                fn(*args, **kwargs)
            except ServiceError as e:
                flash(e.message, "error")
            except sqlite3.OperationalError as e:
                # 等锁超时（busy_timeout 之后仍拿不到写锁）：明确提示，不抛裸 500
                if "locked" in str(e).lower():
                    app.logger.warning("写锁等待超时：%s", e)
                    flash(LOCKED_MESSAGE, "error")
                else:
                    raise
            return redirect(url_for("index"))

        return wrapper

    @app.get("/")
    def index():
        return render_template("index.html", state=services.get_state(get_db()))

    @app.post("/upload")
    @html_view
    def upload():
        fa = request.files.get("file_a")
        fb = request.files.get("file_b")
        if not fa or not fb or not fa.filename or not fb.filename:
            raise ServiceError("请同时选择旧表、新表两个 CSV 文件")
        session_id, header, n_a, n_b = services.load_session(get_db(), fa, fb)
        flash(
            f"已加载：旧表 {n_a} 行、新表 {n_b} 行，共 {len(header)} 列。"
            "此前会话（键、判定、供数）已重置，旧页面操作将提示会话过期。",
            "ok",
        )

    @app.post("/columns")
    @html_view
    def columns():
        cols = request.form.getlist("columns")
        services.set_columns(get_db(), cols, _form_session())
        flash(f"已保存 {len(cols)} 个备选列，请计算对照键。", "ok")

    @app.post("/keys/compute")
    @html_view
    def keys_compute():
        key_ids, found = services.compute_keys(get_db(), _form_session())
        if found:
            flash(
                f"找到 {len(found)} 个列数最少（{len(found[0])} 列）的合规对照键："
                + "；".join(" + ".join(c) for c in found),
                "ok",
            )
        else:
            flash("所选备选列中不存在合规对照键，请调整备选列。", "error")

    @app.post("/keys/select")
    @html_view
    def keys_select():
        services.select_key(get_db(), int(request.form["key_id"]), _form_session())
        flash("已选用对照键，开始供数，可下载行号对照表。", "ok")

    @app.post("/judgments")
    @html_view
    def judgments_add():
        info = services.register_judgment(
            get_db(),
            int(request.form["row_a"]),
            int(request.form["row_b"]),
            request.form["typ"],
            _form_session(),
        )
        flash(_judgment_flash("登记", info), "ok" if info["serving"] else "warn")

    @app.post("/judgments/revoke")
    @html_view
    def judgments_revoke():
        info = services.revoke_judgment(
            get_db(), int(request.form["judgment_id"]), _form_session()
        )
        flash(_judgment_flash("撤销", info), "ok" if info["serving"] else "warn")

    def _form_session():
        return request.form.get("session_id", "")

    def _judgment_flash(action, info):
        parts = [f"判定已{action}。"]
        if info["invalidated"]:
            parts.append("失效键：" + "、".join(f"#{k}" for k in info["invalidated"]) + "。")
        if info["restored"]:
            parts.append("恢复合规键：" + "、".join(f"#{k}" for k in info["restored"]) + "。")
        parts.append("供数中。" if info["serving"] else "已停供，请另选合规键。")
        return "".join(parts)

    @app.get("/download.csv")
    def download():
        try:
            cols, rows, filename = services.build_mapping(
                get_db(), request.args.get("session_id", "")
            )
        except ServiceError as e:
            return Response(e.message, status=e.status, mimetype="text/plain; charset=utf-8")
        return Response(
            services.mapping_csv(cols, rows),
            mimetype="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    # ----------------------------------------------------------- JSON API

    @app.errorhandler(ServiceError)
    def _service_error(e):
        return jsonify(error=e.message), e.status

    @app.errorhandler(sqlite3.OperationalError)
    def _sqlite_locked(e):
        if "locked" in str(e).lower():
            app.logger.warning("写锁等待超时：%s", e)
            return jsonify(error=LOCKED_MESSAGE), 503
        raise e

    @app.post("/api/upload")
    def api_upload():
        fa = request.files.get("file_a")
        fb = request.files.get("file_b")
        if not fa or not fb:
            raise ServiceError("请用 multipart 同时上传 file_a 与 file_b")
        session_id, header, n_a, n_b = services.load_session(get_db(), fa, fb)
        return jsonify(
            ok=True, session_id=session_id, columns=header, rows_a=n_a, rows_b=n_b
        )

    @app.post("/api/columns")
    def api_columns():
        data = request.get_json(force=True)
        services.set_columns(
            get_db(), list(data.get("columns", [])), str(data.get("session_id", ""))
        )
        return jsonify(ok=True)

    @app.post("/api/keys/compute")
    def api_keys_compute():
        data = request.get_json(force=True)
        key_ids, found = services.compute_keys(
            get_db(), str(data.get("session_id", ""))
        )
        return jsonify(ok=True, keys=[{"id": i, "cols": c} for i, c in zip(key_ids, found)])

    @app.post("/api/keys/select")
    def api_keys_select():
        data = request.get_json(force=True)
        services.select_key(
            get_db(), int(data["key_id"]), str(data.get("session_id", ""))
        )
        return jsonify(ok=True, serving=True)

    @app.post("/api/judgments")
    def api_judgments_add():
        data = request.get_json(force=True)
        info = services.register_judgment(
            get_db(),
            int(data["row_a"]),
            int(data["row_b"]),
            str(data["type"]),
            str(data.get("session_id", "")),
        )
        return jsonify(ok=True, **info)

    @app.post("/api/judgments/revoke")
    def api_judgments_revoke():
        data = request.get_json(force=True)
        info = services.revoke_judgment(
            get_db(), int(data["id"]), str(data.get("session_id", ""))
        )
        return jsonify(ok=True, **info)

    @app.get("/api/state")
    def api_state():
        return jsonify(services.get_state(get_db()))

    return app


if __name__ == "__main__":
    create_app().run(
        host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), threaded=True
    )
