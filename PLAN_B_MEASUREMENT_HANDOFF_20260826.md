# Plan B 追加計測 完了引継ぎ（2026-08-26）

## 結論

Plan Bの計画51条件（B1 30、B2 15、B3 6）はすべて収録済みである。全成功runについて、左右NPZ、左Master/右Slaveハードウェア同期、共通カメラ時刻区間、左LED検出、PAT送信、manifestと実ファイルサイズを軽量監査し、異常は検出されなかった。

重い粒子抽出、ステレオ3D再構成、SINDy、科学的妥当性の定量評価は未実施である。全runの`processing_status=pending`は意図どおりで、後処理はスパコンへ移送後に行う。

## 計測の進捗

| 区分 | 計画条件 | 完了条件 | 成功runディレクトリ |
|---|---:|---:|---:|
| B1 小振幅マルチサイン | 30 | 30 | 36 |
| B2 drive A/B/C | 9 | 9 | 9 |
| B2 particle 1/2/3 | 6 | 6 | 6 |
| B3 環境ドリフト | 6 | 6 | 7（本計測6 + pilot 1） |
| 合計 | 51 | 51 | 58 |

B1の36 runには、50%振幅の安全確認1本と、中断・再開に伴うfull-scale成功重複5本を含む。B3の7 runには、旧実装でactive PATからカメラ開始まで約254秒かかった`40_shield_off_warm00_static_60s` pilotを含む。いずれも削除していない。

## 軽量監査結果

| 区分 | 成功run | ファイル数 | 容量 | 左イベント | 右イベント | 監査異常 |
|---|---:|---:|---:|---:|---:|---:|
| B1 | 36 | 626 | 28,523,263,644 bytes | 999,067,889 | 1,000,243,430 | 0 |
| B2 | 15 | 262 | 19,591,611,360 bytes | 690,027,953 | 683,926,552 | 0 |
| B3 | 7 | 127 | 26,563,334,081 bytes | 957,193,485 | 902,100,277 | 0 |

全58 runで確認した項目:

- `capture_complete=true`、`processing_status=pending`
- 左serial `00000508`、右serial `00000509`
- 左Master / 右Slave、hardware sync verified、pair validation errorなし
- 左右とも同一の共通カメラ時刻区間を完走
- 左右NPZが存在し、manifest記録サイズと実ファイルサイズが一致
- イベント上限未到達、capture errorなし
- PAT開始LEDは左、ROI `600,0,1280,180`、検出成功
- `pat_send_error`なし

これはファイル健全性と収録同期の監査であり、粒子応答品質の定量評価ではない。

## B2 particle条件

| condition | particle_id | 収録内容 |
|---|---|---|
| particle1 | `20260826_P01` | static 30 s / z staircase-ringdown |
| particle2 | `20260826_P02` | static 30 s / z staircase-ringdown |
| particle3 | `20260826_P03` | static 30 s / z staircase-ringdown |

各particleのstaticとstaircaseは同じ`particle_id`で記録されている。外観記述は`pipeline_manifest.json > automation.operator_context`を参照すること。

## B3 active-PAT暖機

| label | 目標 | 実測active PAT→camera開始 | 囲い | 空調 | 状態 |
|---|---:|---:|---|---|---|
| `40_shield_off_warm00_static_60s` | 0分 | 0.9610分 | off | on | 本計測・目標達成 |
| `41_shield_off_warm05_static_60s` | 5分 | 5.1391分 | off | on | 目標達成 |
| `42_shield_off_warm10_static_60s` | 10分 | 10.1384分 | off | on | 目標達成 |
| `43_shield_on_warm05_static_60s` | 5分 | 5.1385分 | on | on | 目標達成 |
| `44_shield_on_hvac_off_warm05_static_60s` | 5分 | 5.1387分 | on | off | 目標達成 |
| `45_shield_off_hvac_off_warm05_static_60s` | 5分 | 5.1392分 | off | off | 目標達成 |

事前計算版の6 runはすべて`precomputed_before_pat_output=true`、`static_compacted=true`で、600,000 source frameを1 geometry × 600,000 loopとして60秒再生している。warm00の0分は意図的な待機を加えない「最短開始」を意味し、preview・オペレータ確認・カメラ起動時間は実測値へ含まれる。

旧pilotは`plan_b_identification_000_40_shield_off_warm00_static_3d1f6544b3_20260826_192442`であり、本計測集合から除外する。本計測の40は末尾`20260826_200651`である。

## session監査記録

- B1には中断・再開に伴うfailed sessionがあるが、後続sessionで全条件を取得済みである。
- B2の`auto_recording_session_20260826_173448.json`はparticle1開始時のCtrl+C中断記録で、成功runは作られていない。後続sessionでparticle1の2条件を取得済みである。
- B3の`auto_recording_session_20260826_205018.json`は43の未成功開始記録で、runディレクトリはない。後続`205059` sessionで43を取得済みである。
- 詳細は`PLAN_B_SESSION_AUDIT_20260826.csv`を参照すること。

## 成果物

- `stereo_acoustools_3d_records_plan_b1/`: B1収録一式
- `stereo_acoustools_3d_records_plan_b2/`: B2全15条件
- `stereo_acoustools_3d_records_plan_b3/`: B3本計測6条件、旧40 pilot、session監査記録
- `plan_b_20260825/`: 使用plan、現行export、旧B3 export退避、解析資料
- `plan_b_context.json`: B2/B3のオペレータcontext
- `PLAN_B_RUN_INVENTORY_20260826.csv`: 全58成功runの一覧
- `PLAN_B_SESSION_AUDIT_20260826.csv`: 全sessionの完了・中断・失敗監査表
- `PLAN_B_TRANSFER_FILE_INVENTORY_20260826.csv`: 転送対象の相対パス・byte数
- `PLAN_B_ADDITIONAL_MEASUREMENT_JP.md`: 現行計測手順

## 後処理側への注意

- manifestには計測PC上の絶対パスが残る。移送先ではrunディレクトリ配下の同名ファイルを使うこと。
- `deferred_postprocess_command`はスパコン側のPython環境とパスに読み替えること。
- B1の50%試行をfull-scale同定集合へ混ぜないこと。成功重複5条件の扱いを解析前に固定すること。
- B3旧40 pilotをwarm00本計測へ混ぜないこと。本計測は末尾`20260826_200651`を使うこと。
- 温度は計測不能理由がcontextに記録されており、数値温度による補正はできない。
- 基準仕様は左Master同期、左LED ROI `600,0,1280,180`、既定ステレオ校正、10 kHz PAT指令、Delay feedforwardなしである。

## 転送先

`G:\マイドライブ\Experiment\20260826_2`

計測PCからG:ドライブを読み返し、相対パス、ファイル数、byte数を照合する。Google Driveクラウド側への同期完了状態は別途確認が必要である。

2026-08-26 21:26 JSTに追補転送を完了した。B1 626、B2 262、B3 127、plan 238の計1,253ファイル・75,411,686,604 bytesについて、転送元の全相対パスがG:側に存在し、byte数不一致は0件だった。B2は既存156ファイルを上書きせず、新規106ファイルだけを追加した。B3は127ファイルを新規追加した。

G:側の旧B3 exportは`plan_b_20260825/export_B3_drift_source_pre_active_pat_timer_20260826/`へ退避した。現行plan/exportと旧版の双方を保持している。上記はG:ドライブからの読み返し結果であり、Google Driveクラウドへの同期完了そのものは別途確認が必要である。

同じG:側最終フォルダーを`D:\20260826_2`へミラーした。G:とD:の全内容を比較し、1,294ファイル・75,602,040,966 bytes、相対パス欠落0、D:側余分0、サイズ不一致0を確認した。重要manifest・context・引継ぎ資料7ファイルはSHA-256も一致した。
