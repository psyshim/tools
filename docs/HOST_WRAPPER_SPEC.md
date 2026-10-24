# ホスト側ラッパー 仕様・要件(先方が書く部分)

対象読者: 既存チャットアプリ(FastAPI + React 等)の保守担当。
この文書は `docs/HOST_BACKEND_WRAPPER_REQUIREMENTS.md`(提案段階の旧版)を置き換える。サービス側・device 側の運用は `docs/OPERATOR_GUIDE.md`、1 枚の概要は `docs/HOST_KIT.md`。

## 1. 責務の分担

| こちらが提供(`code-host-kit` に入っている) | 先方が書く |
|---|---|
| `HostEnvironment`(先方が埋めるインターフェイス) | `HostEnvironment` のサブクラス(最小 1 メソッド) |
| `CodingEndpoints.dispatch`(入力検証・ACL・フラグ・エラー変換・ゲートウェイ呼び出し) | 既存フレームワークへのルート登録(`ROUTES` を回すだけ) |
| `ROUTES`(5 本のルート表) | ログイン確認(既存の仕組みで `user` を確定する) |
| `AgentGateway`(サービスとの通信・再試行・冪等キー・イベント整形) | 画面(ボタン、イベント表示、承認カード) |
| `host_client.acceptance`(受け入れ確認ツール) | サービスの接続先 URL と資格情報の保管 |

先方は次を**書かない・足さない**: タスク状態管理、再試行、ファイル・Git・テスト・プロセス実行、モデル呼び出し、イベントの再構成。必要だと感じたらこちらの不具合として扱う(先方に回さない)。

## 2. 先方が実装するインターフェイス

```python
from host_client import HostEnvironment

class MyHost(HostEnvironment):
    def can_access_workspace(self, user, workspace_id):   # 必須
        return my_acl(user, workspace_id)                  # 先方の ACL。サービスへ問い合わせない(ローカル判断)。例外は「不可」扱い。
    def coding_enabled(self, user):                        # 任意。機能フラグ。既定 True。False なら availability 以外は 404 "disabled"
        return flag_on(user)
    def audit(self, user, action, detail):                 # 任意。start / decision / cancel の後に呼ばれる。detail は ID のみ(本文を含まない)。例外は無視される
        my_audit_log(user, action, detail)
```

ログイン確認(`identity`)は先方の既存処理をそのまま使い、確定した `user`(文字列、空でない)を `dispatch` に渡す。ログインしていない要求は `dispatch` を呼ぶ前に 401 で拒否する。

## 3. ルートの組み込み(FastAPI の例。他のフレームワークも同じ形)

```python
from host_client import ROUTES, AgentGateway, CodingEndpoints
endpoints = CodingEndpoints(AgentGateway(SERVICE_URL, SERVICE_CREDENTIAL), MyHost())

for route in ROUTES:                                  # (name, method, path) の 5 本。先方が前置パスを付けてよい
    def make(route):
        async def handler(request):
            user = my_login(request)                  # 先方の既存ログイン
            body = await request.json() if route.method == "POST" else None
            reply = endpoints.dispatch(route.method, route.path.format(**request.path_params), user=user,
                                       query=dict(request.query_params), body=body)
            return JSONResponse(reply.body, status_code=reply.status)
        return handler
    app.add_api_route(route.path, make(route), methods=[route.method])
```

完全な動作例: `examples/sample_host/app.py`(認証・ACL・通常チャット付き)。

## 4. ルート仕様(ブラウザ向け。応答は常に JSON)

| ルート | リクエスト | 成功応答 (200) |
|---|---|---|
| `GET /coding/availability` | なし | `{"available": bool, "reason": str\|null, "modes": ["read","propose",...], "mutation": bool}`。サービスが落ちていても 200(`available:false`) |
| `POST /coding/start` | `{"workspace_id","text","conversation_id","message_id"}`(全て非空文字列。`text` ≤ 20000、他 ≤ 128 文字) | `{"ok":true,"handle":"<不透明ID>"}`。同じ `conversation_id`+`message_id` は同じ handle(再送・二重クリックで二重起動しない) |
| `GET /coding/{handle}/events?after=<cursor>` | `after` は前回の `next`(初回 `0`) | `{"ok":true,"terminal":bool,"next":"<cursor>","events":[{"id","type","html","synthetic","data"}]}` |
| `POST /coding/{handle}/decision` | `{"action_id","fingerprint","approve": true\|false}`(`approve` は真偽値) | `{"ok": true}`(受理)。受理されない場合(古いカード・指紋不一致・回答済み)は 409 `{"ok":false,"error":"no_pending_action","already_decided":bool}` |
| `POST /coding/{handle}/cancel` | `{}` | `{"ok":true,"state":"CANCELLED"\|...}`(既に終了していればその状態) |

エラー応答は `{"ok":false,"error":"<token>", "reason"?:...}`。ステータスは次の対応で固定:

| error | HTTP | 意味 |
|---|---|---|
| `forbidden` | 403 | 先方の ACL が拒否(サービスへは問い合わせない) |
| `disabled` | 404 | 機能フラグ OFF |
| `not_found` | 404 | 存在しない、または他人のタスク(区別できない) |
| `bad_request` / `invalid_request` | 400 | 入力不正(型・長さ・カーソル) |
| `no_pending_action` / `conflict` | 409 | 承認カードが既に処理済み・指紋不一致など |
| `busy` / `rate_limited` | 429 | 同時実行数の上限など |
| `unavailable` | 503 | サービス停止・到達不能・契約違反・別メジャー版。`reason` は短い定型語 |
| その他 | 502 | サービスが未知の応答を返した |

## 5. イベントと画面の規則

`events[].type` は次のいずれか: `assistant_message` / `tool_activity` / `proposal_completed` / `approval_required` / `approval_decision` / `mutation_applied` / `test_result` / `notice` / `state` / `gap`。

- `html` は整形・長さ制限・エスケープ済み。**表示にはこれを使い、`data` の文字列を未エスケープで HTML に入れない。**
- `id` は増加。`synthetic:true`(欠落表示 `gap`)は画面に「ここに欠落あり」と出す。重複 `id` は 1 回だけ表示する。
- `terminal:true` で `state` が `COMPLETED` / `FAILED` / `CANCELLED` / `NEEDS_RECONCILIATION` のいずれか。`NEEDS_RECONCILIATION` は「結果が確認できない。運用担当に連絡」と表示し、成功扱いにしない。
- **`proposal_completed` は「ファイルが変わった」を意味しない。** 変更は `mutation_applied` が出たときだけ。既定の構成では `mutation_applied` は出ない。
- `approval_required` の `data` に `action_id` / `fingerprint` / `summary`(差分など)があり、承認ボタンはそれをそのまま `decision` に送る。承認 UI を作らないなら、`availability.mutation` が `false` の構成にしておく(承認カードは出ない)。**書き込み・コマンド・コミットを自動承認してはならない。**
- 画面側は `next` を保持して再接続でも続きから取得する(`after=<next>`)。

## 6. 必須要件(MUST)

1. サービス資格情報(`SERVICE_CREDENTIAL`)はサーバー側にだけ置き、ブラウザへ出さない。サービスとは TLS(社内で loopback 以外の場合)。
2. 既存のチャット・ログイン・アップロードは、コーディング機能が無い・止まっている・壊れている場合も動き続ける(`unavailable` を見せるだけ)。機能フラグは独立して OFF にできる。
3. ユーザーの認可は先方で行う。ブラウザが送る `workspace_id` / `handle` を信用しない(`workspace_id` は `can_access_workspace`、他人の `handle` はサービスが 404 を返す)。
4. 絶対パス・個人情報・メールアドレスをログ、画面、監査に出さない(イベントはワークスペース相対パスのみ)。
5. 承認の自動化をしない。`approve:true` は人が承認ボタンを押したときだけ送る。
6. 要求・応答の大きさを先方側でも制限する(`dispatch` が文字列長を検証する)。
7. 再試行を足さない(冪等キーでサービス側が二重起動を防ぐ)。

## 7. 推奨(SHOULD)

- 利用者ごとの同時実行数・日次回数の上限を先方の既存クォータに合わせる。
- `audit` で ID のみを監査ログに残す(本文は残さない)。
- 画面は 1〜2 秒間隔のポーリングで足りる(SSE は不要)。

## 8. テスト(先方の既存テスト実行にそのまま組み込める)

### 8.1 ユニットテスト一式 `host_client.testing.WrapperContract`(ネットワーク不要・実サービス不要)

キットに偽のコーディングサービスが同梱されているので、先方の CI で、実サービスなしに、ラッパーの契約を 17 項目検査できる。先方が書くのは「ユーザーを指定してアプリを呼ぶ」小さなハーネスと 4 つの定数だけ。

```python
import unittest
from host_client.testing import WrapperContract

class MyWrapperTests(WrapperContract, unittest.TestCase):
    USER_A, USER_B = "alice", "bob"                 # 先方のログインが返すユーザー ID
    WORKSPACE_A, WORKSPACE_B = "w-alice", "w-bob"   # A は前者だけ、B は後者だけ使える(先方の ACL 設定)
    CHAT = ("POST", "/chat", {"message": "hi"})     # 任意: 既存チャット(サービス停止中も動くことを検査)

    def make_host(self, gateway):                   # 渡された gateway を使うようにラッパーを組み立てて返す
        return MyHarness(build_my_app(gateway))

class MyHarness:
    def request(self, method, path, user, body=None, query=None):
        ...   # user としてアプリを呼び (status, json) を返す。user=None は未ログイン
```

完全なテンプレート: `tests/test_sample_host_contract.py`(サンプルホスト用。ここをコピーして `make_host` を差し替える)。

検査項目(失敗したテスト名が破った規則を示す): 未ログイン拒否 / availability の形 / サービス停止時の 503 とチャット無影響 / 資格情報拒否・HTML・不正応答でも 503 で漏えいなし / 二重起動なし / ACL はローカルで 403 / 不正入力は 400 / 利用者 ID の伝達 / 他人の読み取り・取消・承認不可 / 不明 handle / イベントの順序・再開・終了 / 不正カーソル / 敵対的テキストのエスケープと長さ制限 / 承認カードの内容と決定の厳密さ / 取消の冪等 / 不明ルート / 上限超えイベントの拒否。
全応答に対し、絶対パスとサービス資格情報が含まれないことも自動で検査する。

このスイート自体の品質確認として、わざと壊したラッパー(ACL 全許可、全員が同一ユーザー扱い、未ログイン通過、イベント本文の生表示、サービス停止時に 500)が、それぞれ該当する検査で必ず失敗することをテストしている。

### 8.2 受け入れ確認 `host_client.acceptance`(動いているバックエンドに対する外部検査)

書き終えたら実行する。

テスト用に別々の 2 ユーザー、ユーザー A が使える workspace と使えない workspace を用意して実行する。

```bash
python -m host_client.acceptance --base https://chat.example/api \
    --token-a <ユーザーAのトークン> --token-b <ユーザーBのトークン> \
    --workspace-a <Aが使える> --workspace-denied <Aが使えない> \
    --text "list the project files" --chat-path /chat --output acceptance.json
```

検査内容: 未ログインの拒否、可用性の応答形状、使えない workspace の 403(サービス呼び出しなしで即時)、不正入力が 400 になること(500 でない)、同じ `message_id` が同じ handle になること、イベントの順序・エスケープ・終了、他ユーザーが読み取り・取消・承認できないこと、不正カーソルと不明 handle、通常チャットへの無影響。
`SKIP` は合格ではない(サービスを止めた状態で確認したい場合のみ `--allow-skip`)。`FAIL` が 1 つでもあれば終了コード 1。出力 JSON は実行時のリビジョンと一緒に保管する。

## 9. こちらで用意した範囲と、まだ検証できていない範囲

- 検証済み: キット単体(`dispatch` の検証・ACL・フラグ・監査・エラー変換)、サンプルホスト、受け入れツール(正しいホストは全 PASS、ACL 欠落や他人タスク漏えいのホストは FAIL)。
- **未検証: 先方の実アプリでの組み込み、実画面(React)での承認カード表示、先方のログイン・ACL・クォータとの結合、先方ネットワークでの 3 プロセス運用。** これらは上の受け入れツールと、先方環境での実行で初めて確認できる。
- モデル呼び出しはサービス側の設定(`model`)で行う。先方の ChatGPT/Claude 併用の仕組みを流用する場合は、`complete(messages, tools) -> {"text", "tool_calls"}` の関数を 1 つ用意してサービス設定に指定する(`docs/OPERATOR_GUIDE.md`)。
