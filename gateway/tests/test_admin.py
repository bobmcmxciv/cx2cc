from aiohttp.test_utils import TestClient

KEY = "sk-cx2cc-test-key-aaaaaaaaaaaaaaaaaaaaaaaaaaaa"
PASSWORD = "correct-horse-battery"


async def login(client: TestClient, username: str, password: str = PASSWORD) -> str:
    r = await client.post("/admin/api/login", json={"username": username, "password": password})
    assert r.status == 200, await r.text()
    return (await r.json())["me"]["csrf"]


async def new_client(gw, aiohttp_client):
    return await aiohttp_client(gw.client.server)


async def test_console_requires_login_and_serves_ui(gw):
    r = await gw.client.get("/admin/api/keys")
    assert r.status == 401
    r = await gw.client.get("/admin/")
    assert r.status == 200 and "text/html" in r.headers["Content-Type"]
    assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]
    r = await gw.client.get("/admin/static/app.js")
    assert r.status == 200 and r.headers["Content-Type"].startswith("text/javascript")
    r = await gw.client.get("/admin/static/..%2Fadmin.py")
    assert r.status == 404


async def test_login_failure_lockout(gw):
    gw.add_user("root", "admin")
    for _ in range(10):
        r = await gw.client.post("/admin/api/login", json={"username": "root", "password": "wrong-password"})
        assert r.status == 401
    r = await gw.client.post("/admin/api/login", json={"username": "root", "password": PASSWORD})
    assert r.status == 429
    events = gw.db.all("SELECT action FROM audit_events")
    assert [e["action"] for e in events].count("login.failed") == 10


async def test_csrf_is_enforced(gw):
    gw.add_user("root", "admin")
    csrf = await login(gw.client, "root")
    r = await gw.client.post("/admin/api/keys", json={"alias": "a1"})
    assert r.status == 403
    r = await gw.client.post("/admin/api/keys", json={"alias": "a1"}, headers={"X-CSRF-Token": csrf})
    assert r.status == 201


async def test_admin_creates_key_that_works_and_is_audited(gw):
    gw.add_user("root", "admin")
    csrf = await login(gw.client, "root")
    r = await gw.client.post(
        "/admin/api/keys", headers={"X-CSRF-Token": csrf},
        json={"alias": "bob-mac", "owner": "bob", "note": "MacBook", "rpm_limit": 60,
              "allowed_models": "gpt-6.1-sol, gpt-6-luna"},
    )
    assert r.status == 201
    data = await r.json()
    secret = data["key"]
    assert secret.startswith("sk-cx2cc-") and data["record"]["scopes"] == ["chat", "images", "usage"]
    assert data["record"]["allowed_models"] == ["gpt-6.1-sol", "gpt-6-luna"]
    stored = gw.db.one("SELECT * FROM api_keys WHERE alias = 'bob-mac'")
    assert secret not in str(stored)

    r = await gw.client.post("/api/v1/messages", headers={"x-api-key": secret},
                             json={"stream": True, "messages": []})
    assert r.status == 200
    await r.text()
    gw.rt.recorder.flush()

    r = await gw.client.get("/admin/api/keys")
    [item] = (await r.json())["keys"]
    assert item["alias"] == "bob-mac" and item["usage"]["requests_today"] == 1
    assert item["usage"]["weighted_today"] == 5400 and item["can_manage"]

    r = await gw.client.get(f"/admin/api/keys/{item['id']}")
    detail = await r.json()
    assert detail["daily"][0]["requests"] == 1 and detail["models"][0]["model"] == "gpt-6.1-sol"

    r = await gw.client.get("/admin/api/requests", params={"key_id": item["id"]})
    [req] = (await r.json())["requests"]
    assert req["weighted"] == 5400

    r = await gw.client.get("/admin/api/overview")
    ov = await r.json()
    assert ov["totals"]["today"]["requests"] == 1 and ov["by_key"][0]["alias"] == "bob-mac"
    assert ov["by_owner"][0]["owner"] == "bob"

    r = await gw.client.get("/admin/api/events", params={"target_type": "key"})
    actions = [e["action"] for e in (await r.json())["events"]]
    assert "key.create" in actions


async def test_duplicate_alias_rejected(gw):
    gw.add_user("root", "admin")
    csrf = await login(gw.client, "root")
    h = {"X-CSRF-Token": csrf}
    assert (await gw.client.post("/admin/api/keys", headers=h, json={"alias": "Same"})).status == 201
    r = await gw.client.post("/admin/api/keys", headers=h, json={"alias": "same"})
    assert r.status == 409


async def test_operator_sees_only_own_keys_and_cannot_grant_accounts(gw, aiohttp_client):
    gw.add_user("root", "admin")
    gw.add_user("ops", "operator")
    admin = gw.client
    admin_csrf = await login(admin, "root")
    r = await admin.post("/admin/api/keys", headers={"X-CSRF-Token": admin_csrf}, json={"alias": "admins-key"})
    admin_key_id = (await r.json())["record"]["id"]

    op = await new_client(gw, aiohttp_client)
    csrf = await login(op, "ops")
    h = {"X-CSRF-Token": csrf}
    r = await op.post("/admin/api/keys", headers=h, json={"alias": "ops-key", "scopes": ["chat", "accounts"]})
    assert r.status == 403
    r = await op.post("/admin/api/keys", headers=h, json={"alias": "ops-key", "scopes": ["chat"]})
    assert r.status == 201
    own_id = (await r.json())["record"]["id"]
    r = await op.get("/admin/api/keys")
    assert [k["alias"] for k in (await r.json())["keys"]] == ["ops-key"]
    assert (await op.get(f"/admin/api/keys/{admin_key_id}")).status == 404
    assert (await op.post(f"/admin/api/keys/{admin_key_id}/disable", headers=h, json={})).status == 404
    assert (await op.post(f"/admin/api/keys/{own_id}/disable", headers=h, json={})).status == 200
    assert (await op.get("/admin/api/users")).status == 403
    assert (await op.patch("/admin/api/settings", headers=h, json={"retention_days": 30})).status == 403


async def test_auditor_reads_everything_but_cannot_create(gw, aiohttp_client):
    gw.add_user("audit", "auditor")
    gw.add_key("someone", KEY)
    c = await new_client(gw, aiohttp_client)
    csrf = await login(c, "audit")
    r = await c.get("/admin/api/keys")
    [k] = (await r.json())["keys"]
    assert k["alias"] == "someone" and not k["can_manage"]
    r = await c.post("/admin/api/keys", headers={"X-CSRF-Token": csrf}, json={"alias": "x"})
    assert r.status == 403
    r = await c.post(f"/admin/api/keys/{k['id']}/revoke", headers={"X-CSRF-Token": csrf}, json={})
    assert r.status == 403


async def test_key_holder_portal_sees_only_own_key(gw, aiohttp_client):
    mine = gw.add_key("mine", KEY)
    theirs = gw.add_key("theirs", "sk-cx2cc-test-key-zzzzzzzzzzzzzzzzzzzzzzzzzzzz")
    await gw.client.post("/api/v1/messages", headers={"x-api-key": KEY}, json={"messages": []})
    await gw.client.post("/api/v1/messages", headers={"x-api-key": "sk-cx2cc-test-key-zzzzzzzzzzzzzzzzzzzzzzzzzzzz"},
                         json={"messages": []})
    await gw.client.post("/api/v1/messages", headers={"x-api-key": "bogus-bogus-bogus"}, json={"messages": []})
    gw.rt.recorder.flush()
    c = await new_client(gw, aiohttp_client)
    r = await c.post("/admin/api/login-key", json={"key": KEY})
    assert r.status == 200
    me = (await r.json())["me"]
    assert me["kind"] == "key" and me["name"] == "mine"
    r = await c.get("/admin/api/keys")
    assert [k["id"] for k in (await r.json())["keys"]] == [mine]
    assert (await c.get(f"/admin/api/keys/{theirs}")).status == 404
    r = await c.get("/admin/api/requests")
    assert {x["key_id"] for x in (await r.json())["requests"]} == {mine}
    r = await c.post(f"/admin/api/keys/{mine}/disable", headers={"X-CSRF-Token": me["csrf"]}, json={})
    assert r.status == 403
    r = await c.post("/admin/api/login-key", json={"key": "sk-cx2cc-wrong"})
    assert r.status == 401


async def test_rotate_with_and_without_grace(gw):
    gw.add_user("root", "admin")
    csrf = await login(gw.client, "root")
    h = {"X-CSRF-Token": csrf}
    r = await gw.client.post("/admin/api/keys", headers=h, json={"alias": "rot"})
    data = await r.json()
    old, kid = data["key"], data["record"]["id"]

    r = await gw.client.post(f"/admin/api/keys/{kid}/rotate", headers=h, json={"grace_hours": 24})
    rotated = await r.json()
    new = rotated["key"]
    assert new != old and rotated["record"]["rotation_grace_until"]
    for secret in (old, new):
        r = await gw.client.post("/api/v1/messages", headers={"x-api-key": secret}, json={"messages": []})
        assert r.status == 200

    r = await gw.client.post(f"/admin/api/keys/{kid}/rotate", headers=h, json={"grace_hours": 0})
    newest = (await r.json())["key"]
    r = await gw.client.post("/api/v1/messages", headers={"x-api-key": new}, json={"messages": []})
    assert r.status == 401
    r = await gw.client.post("/api/v1/messages", headers={"x-api-key": newest}, json={"messages": []})
    assert r.status == 200


async def test_revoke_is_final(gw):
    gw.add_user("root", "admin")
    csrf = await login(gw.client, "root")
    h = {"X-CSRF-Token": csrf}
    data = await (await gw.client.post("/admin/api/keys", headers=h, json={"alias": "bye"})).json()
    kid = data["record"]["id"]
    assert (await gw.client.post(f"/admin/api/keys/{kid}/revoke", headers=h, json={})).status == 200
    assert (await gw.client.post(f"/admin/api/keys/{kid}/enable", headers=h, json={})).status == 409
    r = await gw.client.post("/api/v1/messages", headers={"x-api-key": data["key"]}, json={"messages": []})
    assert r.status == 401


async def test_update_key_and_audit_diff(gw):
    gw.add_user("root", "admin")
    csrf = await login(gw.client, "root")
    h = {"X-CSRF-Token": csrf}
    data = await (await gw.client.post("/admin/api/keys", headers=h, json={"alias": "edit-me"})).json()
    kid = data["record"]["id"]
    r = await gw.client.patch(f"/admin/api/keys/{kid}", headers=h,
                              json={"alias": "edited", "daily_limit": 1000, "owner": "carol"})
    rec = (await r.json())["record"]
    assert rec["alias"] == "edited" and rec["daily_limit"] == 1000
    ev = gw.db.one("SELECT * FROM audit_events WHERE action = 'key.update'")
    assert '"edit-me"' in ev["detail"] and '"carol"' in ev["detail"]


async def test_users_management_keeps_one_admin(gw):
    root = gw.add_user("root", "admin")
    csrf = await login(gw.client, "root")
    h = {"X-CSRF-Token": csrf}
    r = await gw.client.patch(f"/admin/api/users/{root}", headers=h, json={"role": "auditor"})
    assert r.status == 409
    r = await gw.client.post("/admin/api/users", headers=h,
                             json={"username": "alice", "role": "operator", "password": "short"})
    assert r.status == 400
    r = await gw.client.post("/admin/api/users", headers=h,
                             json={"username": "alice", "role": "operator", "password": "long-enough-pass"})
    assert r.status == 201


async def test_password_change_and_logout(gw):
    gw.add_user("root", "admin")
    csrf = await login(gw.client, "root")
    h = {"X-CSRF-Token": csrf}
    r = await gw.client.post("/admin/api/password", headers=h,
                             json={"old_password": "nope", "new_password": "another-long-pass"})
    assert r.status == 400
    r = await gw.client.post("/admin/api/password", headers=h,
                             json={"old_password": PASSWORD, "new_password": "another-long-pass"})
    assert r.status == 200
    assert (await gw.client.post("/admin/api/logout", headers=h, json={})).status == 200
    assert (await gw.client.get("/admin/api/keys")).status == 401
    await login(gw.client, "root", "another-long-pass")


async def test_csv_export(gw):
    gw.add_user("root", "admin")
    gw.add_key("csv", KEY)
    await gw.client.post("/api/v1/messages", headers={"x-api-key": KEY}, json={"messages": []})
    gw.rt.recorder.flush()
    await login(gw.client, "root")
    r = await gw.client.get("/admin/api/requests.csv")
    assert r.status == 200 and "attachment" in r.headers["Content-Disposition"]
    text = (await r.read()).decode("utf-8-sig")
    lines = text.strip().splitlines()
    assert lines[0].startswith("id,rid,ts,time") and len(lines) == 2 and ",csv," in lines[1]


async def test_settings_change_weights(gw):
    gw.add_user("root", "admin")
    csrf = await login(gw.client, "root")
    r = await gw.client.patch("/admin/api/settings", headers={"X-CSRF-Token": csrf},
                              json={"weight_output": 4, "retention_days": 90})
    assert r.status == 200
    assert (await r.json())["settings"]["weight_output"] == 4.0
    assert gw.rt.settings.w_output == 4.0


async def test_upstream_view_strips_local_paths(gw):
    gw.add_user("root", "admin")
    await login(gw.client, "root")
    r = await gw.client.get("/admin/api/upstream")
    data = await r.json()
    [acct] = data["accounts"]["accounts"]
    assert acct["id"] == "pro-1" and "path" not in acct
    assert data["catalog"]["aliases"] == {"gpt-6-sol": "gpt-6.1-sol"}
