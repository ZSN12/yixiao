# -*- coding: utf-8 -*-
"""飞书一键登录(feishu_oauth) pytest 单测。

覆盖:
1. build_authorize_url: 具备 app_id+回调时返回 enabled=True + url + 一次性 state;
2. build_authorize_url: 未配置时返回 enabled=False + reason;
3. authenticate_with_code: 校验一次性 state(错误/已消费 state 失败);
4. authenticate_with_code: 防重放(同一 code 二次使用失败);
5. authenticate_with_code: 完整成功路径(monkeypatch 换 token + 查用户信息) -> open_id;
6. user_auth.issue_session: 可签发销售/超管会话。
"""

from modules import feishu_oauth, user_auth


def _patch_network(monkeypatch):
    """monkeypatch 飞书 OAuth 网络调用, 返回固定用户信息。"""
    monkeypatch.setattr(feishu_oauth, "_exchange_code_for_token", lambda code: "mock_access_token")
    monkeypatch.setattr(
        feishu_oauth,
        "get_user_info_by_token",
        lambda token: {
            "open_id": "ou_feishu_test001",
            "name": "测试销售",
            "en_name": "Test",
            "avatar_url": "https://x/avatar.png",
            "mobile": "13800000001",
        },
    )


def test_build_authorize_url_enabled(monkeypatch):
    """具备 app_id 与回调地址 -> enabled=True + url + state。"""
    monkeypatch.setattr(feishu_oauth.settings, "feishu_app_id", "cli_test_app")
    monkeypatch.setattr(feishu_oauth.settings, "feishu_webapp_url", "https://test.example.com")
    r = feishu_oauth.build_authorize_url()
    assert r.get("enabled") is True
    assert "accounts.feishu.cn" in r["url"]
    assert "app_id=cli_test_app" in r["url"]
    assert "state=" in r["url"]
    assert r.get("state")
    # callback 指向前端 oauth 页
    assert r.get("callback", "").endswith("#/feishu-oauth")


def test_build_authorize_url_disabled_missing_app(monkeypatch):
    """未配置 app_id -> enabled=False + reason。"""
    monkeypatch.setattr(feishu_oauth.settings, "feishu_app_id", "")
    r = feishu_oauth.build_authorize_url()
    assert r.get("enabled") is False
    assert "reason" in r


def test_authenticate_state_invalid(monkeypatch):
    """state 未下发/错误 -> 认证失败。"""
    _patch_network(monkeypatch)
    r = feishu_oauth.authenticate_with_code("some_code", "bad_state")
    assert r.get("ok") is False
    assert "state" in r.get("reason", "") or "授权" in r.get("reason", "")


def test_authenticate_success_and_replay(monkeypatch):
    """完整成功路径: 拿到 open_id; 同一 code 重放被拒。"""
    monkeypatch.setattr(feishu_oauth.settings, "feishu_app_id", "cli_test_app")
    monkeypatch.setattr(feishu_oauth.settings, "feishu_app_secret", "secret")
    monkeypatch.setattr(feishu_oauth.settings, "feishu_webapp_url", "https://test.example.com")
    _patch_network(monkeypatch)

    # 先取一个合法 state
    st = feishu_oauth.build_authorize_url()["state"]
    # 成功
    r = feishu_oauth.authenticate_with_code("code_abc", st)
    assert r.get("ok") is True
    assert r["open_id"] == "ou_feishu_test001"
    assert r["name"] == "测试销售"
    assert r["mobile"] == "13800000001"

    # 同一 code 二次使用(先取新 state) -> 重放被拒
    st2 = feishu_oauth.build_authorize_url()["state"]
    r2 = feishu_oauth.authenticate_with_code("code_abc", st2)
    assert r2.get("ok") is False
    assert "已使用" in r2.get("reason", "")


def test_issue_session_sales():
    """按销售身份签发会话 token。"""
    d = user_auth.issue_session("S001", "sales", "张伟")
    assert d.get("token")
    assert d["username"] == "S001"
    assert d["role"] == "sales"
    assert d["display_name"] == "张伟"
    # 校验 token 有效
    sess = user_auth.get_session(d["token"])
    assert sess is not None
    assert sess["role"] == "sales"
