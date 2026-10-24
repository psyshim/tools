# 納品ポリシー — 何をおさえ、どう守るか

部品として納品するとき、先方に約束するのは「機能」ではなく**不変条件**です。ここに書いた項目は、破る変更が機械的に止まるようにしてあります(止まらないものは「守れていない」と明記します)。

## 1. 不変条件と、それを守る仕組み

| # | 不変条件 | 守る仕組み(破ると赤くなる所) |
|---|---|---|
| 1 | ホスト側に権限を持ち込まない。キットは標準ライブラリ+`protocol`のみ。実行・ファイル・Git・モデルのコードを含まない | `release_gate`「キットに強力な物も個人情報も無い」、`tests/test_host_client.py`(空環境で `-S` 起動)、`tests/test_release_gate.py` |
| 2 | 既定は変更不能。変更系は service と device の**両側**が明示許可したときだけ存在する | `release_gate`「既定の構成では何も変更できない」、`tests/test_experimental_mutation_stack.py`(片側のみ許可の否定テスト)、`tests/test_code_attach_posture.py` |
| 3 | 絶対パス・個人名をイベント/ログ/画面に出さない(パスの全表記を対象。Windows の 8.3 短縮名を含む) | `tests/test_path_spellings.py`、`tests/test_code_attach_privacy.py`、敵対テスト。**限界:** プロジェクトのファイル内容に書かれたパスは内容として扱われる |
| 4 | 嘘の成功を返さない(`proposal_completed` は変更ではない、結果不明は `NEEDS_RECONCILIATION`、テストの偽 PASS なし) | `tests/test_experimental_mutation_stack.py`(クラッシュ地点)、`tests/test_effect_semantics.py` |
| 5 | 外せる。キットを外す・サービスが止まっても既存のチャットは動く | `host_client.testing.WrapperContract`、`host_client.acceptance` |
| 6 | 契約と版の整合。別メジャー版は閉じて失敗する | `tests/test_protocol_contract.py`、`tests/test_protocol_conformance.py`(モック・実サービス・別プロセスで同一シナリオ) |
| 7 | 文書は実装と一致する(設定例は実ローダーで受理され、参照ファイルは実在する) | `release_gate`「運用ガイドの設定例」「文書の参照先」 |
| 9 | 軽い・速い(予算内)。キットは 100 KiB 以下で依存ゼロ、ラッパーは 1 呼び出し 50 ms 以内、実プロセス構成でも開始・最初のイベント・ポーリングが予算内 | `hostkit_build/perf_budgets.json`、`release_gate`「速度と重さが予算内」、`tests/test_perf_budgets.py`。実測値は受領証の `performance` に記録 |
| 8 | 何を納品したか追跡できる(リビジョン・キットのハッシュ・再現可能ビルド) | `release_gate` が `delivery_receipt.json` を出力。キットは 2 回ビルドして同一ハッシュを要求 |

## 2. 納品の定義

次の**すべて**が満たされたリビジョンだけを納品する。

1. `main` へは必須チェックが緑の PR だけが入る(ブランチ保護)。
2. そのリビジョンで(速度・重さの予算を含め) `python -m hostkit_build.release_gate --full` が **Linux と Windows の両方**で `DELIVERABLE`(終了コード 0)。CI の `release-gate` ジョブが実行し、`delivery_receipt.json` を成果物として保存する。
3. キット(`code_host_kit-<版>.whl`)のハッシュが、2 つの OS の受領証で一致している(同一の再現可能ビルド)。
4. 受領証の `not_covered`(下記)を先方に伝えてある。

`--allow-dirty` や `--full` なしの実行結果は、納品の根拠にならない(受領証に `delivery_ok:false` と記録される)。

### 2.1 速度・軽快さの予算(`hostkit_build/perf_budgets.json`)

| 項目 | 予算 | この PC での実測(参考) |
|---|---|---|
| キット wheel / 展開後 | 100 / 300 KiB | 28 / 75 KiB |
| キットの import(最良値) | 3000 ms | 約 330 ms(環境で 300〜1100 ms と大きくぶれる) |
| ラッパー 1 呼び出し p95(開始 / イベント) | 50 ms | 0.7 / 1.3 ms |
| 2 プロセス構成の準備完了 | 60 秒 | 約 19 秒(テスト用ハーネスの値) |
| 構成後の最初の要求 | 5000 ms | 約 580 ms(サービス起動後の一回限りの費用) |
| 最初のイベントまで | 1000 ms | 約 10 ms |
| 2 回目以降のタスク開始(最良値) | 500 ms | 約 20 ms |
| ポーリング p95 | 500 ms | 約 70 ms |
| 納品ゲート全体(並列 2 群) | 30 分 | 約 10 分 |

予算は**桁違いの劣化を止めるための緩い上限**で、ベンチマークではない(アンチウイルスや共有 CI で時間は大きくぶれるため)。最良値または p95 で判定し、生の測定値は受領証に残して、予算に達する前の緩やかな劣化も見えるようにする。予算を締めるのは、複数の受領証で裏づけがあるときだけ。

## 3. 互換性の約束

- **プロトコル** `1.x`: 同じメジャー内ではフィールドの追加のみ。削除・意味変更はメジャーを上げ、旧メジャーのクライアントは `Unavailable` で閉じて失敗する(黙って誤動作しない)。
- **キット** `code-host-kit`: 先方が書く面(`HostEnvironment`、`ROUTES`、`CodingEndpoints.dispatch`、`WrapperContract`、受け入れツール)の破壊的変更はメジャー、追加はマイナー。
- **ルート仕様**(`docs/HOST_WRAPPER_SPEC.md` 4 章)のエラートークンと HTTP ステータスの対応は固定。変更はキットのマイナー以上で、受け入れツールと `WrapperContract` を同時に更新する。

## 4. この仕組みでは守れないもの(受領証の `not_covered` と同じ)

- 先方の実アプリ内での挙動(ログイン、ACL、UI、クォータ)— e2e でしか確認できない。
- React の承認カードの実ブラウザ表示。
- 実モデルの品質(検証はスクリプト化したモデルのみ)。
- POSIX での人間の同時保存、Windows でのリネーム上書き保存(既知の残余リスクとして文書化済み)。
- 先方ネットワークでの 3 プロセスの監視・再起動。

## 5. 変更するときのルール

- 不変条件 1・2・3 に関わるテストを**弱める変更**(期待値の緩和、`skip` の追加、除外リストへの追加)は、PR 説明に理由を書く。
- 既定値(`allow_mutation`、`experimental_mutation`、`allow_remote_clients`)の変更は `release_gate` が止める。意図した変更ならゲートのテスト(`tests/test_release_gate.py`)も同じ PR で更新する。
- CI が赤のままマージしない。赤の原因が環境由来でも、まず再現テストで原因を示す。

## 6. 必須チェック(ブランチ保護で `main` に設定する項目)

`release-gate (ubuntu-latest)`、`release-gate (windows-latest)`、`python-tests (3.11)`、`python-tests (3.12)`、`adversarial (ubuntu-latest)`、`adversarial (windows-latest)`、`windows-processes`。
