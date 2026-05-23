# BH Safe-Mode 100 场景测试 spec

每条:`ID | type | category | name | spec | expected | mode`
- type: auto(test runner 跑)/ manual(人工 / 跨进程 / 跨 chrome 重启)/ review(代码审计可结论)
- mode: 各场景需要的 env 变量(默认 BH_SAFE_MODE=1)

---

## A. 基础导航 / globals 注入(10)
- A01 | auto | navigation | safe globals 注入 | 检查 `agent_tab/goto/eval_js/snap/shot/click_at/type_text/send_keys/fill/upload/close_tab` 都在 globals | 11 个名都存在 |
- A02 | auto | navigation | goto 真实 URL | `goto("https://example.org/")` + `eval_js("location.href")` | 返回 example.org |
- A03 | auto | navigation | goto 同 tab 多次 | goto A → goto B → eval_js 取 url | url=B,agent_tab tid 不变 |
- A04 | auto | navigation | new_tab() 已 raise | 调 `new_tab("https://x.com")` 应 RuntimeError | 抛错且消息含 "disabled" |
- A05 | auto | navigation | goto_url() 已 raise | 同上 | 抛错 |
- A06 | auto | navigation | goto 默认 timeout | goto 一个慢响应 URL | 不卡死 ≤15s |
- A07 | auto | navigation | goto 超时自定义 | `goto(url, timeout=2)` | 2s 后返回(可能是部分加载) |
- A08 | auto | navigation | goto 非法 URL | `goto("not-a-url")` | 不崩,可能加载失败 |
- A09 | auto | navigation | goto 后 eval_js 有效 | `goto("https://example.com"); eval_js("1+1")` | 2 |
- A10 | auto | navigation | goto data URL | `goto("data:text/html,<h1>hi</h1>")` | eval_js("document.querySelector('h1').textContent") = "hi" |

## B. agent_tab 复用 / PID 隔离(10)
- B01 | auto | reuse | 同 PID 多次 ensure 同 tab | bootstrap 后 ensure_agent_tab() 再调返回相同 tid | tid 一致 |
- B02 | auto | reuse | bootstrap 跨 import 幂等 | safe_globals() 调 2 次 | 同 agent_tab |
- B03 | manual | pid | 并发 BH 互不踩 | 两个 BH 进程同时 bootstrap | 各自 agent_tab,STATE_FILE 各自 claim |
- B04 | manual | pid | atexit PID 范围 | A 进程退出不关 B 进程 placeholder | B 的 tab 仍在 |
- B05 | review | pid | 死 PID 占位被新会话清 | _gc_orphan_claims 释放死 PID claim | code path 156-160 |
- B06 | auto | reuse | bootstrap 不 navigate 的 agent_tab atexit 被关 | agent_tab 仍是 example.com placeholder → 退出 | 下次列表无此 tid |
- B07 | auto | reuse | bootstrap navigate 后 atexit 不关 | goto real_url → 退出 | 下次列表此 tid 仍在 |
- B08 | manual | pid | 主 window tab 永远不被关 | 主 window 任何 example.com tab | atexit 无视(URL 无 marker) |
- B09 | auto | reuse | close_tab() 主动关 | close_tab() 后 ensure_agent_tab 拿新 tid | tid 不同 |
- B10 | review | pid | STATE_FILE 锁竞争 | 两 PID 同时写 | 当前无文件锁,潜在风险 |

## C. atexit cleanup(10)
- C01 | auto | atexit | 占位 tab 退出被关 | 不调 goto,exit | 下次启动 placeholder count 减 1 |
- C02 | auto | atexit | 真实 URL tab 退出留下 | goto real → exit | 下次列表 tid 仍在,URL 不变 |
- C03 | auto | atexit | BH_KEEP_PLACEHOLDERS=1 关闭清理 | env 设 1 + 占位 | 下次启动 placeholder count 不变 |
- C04 | auto | atexit | atexit 异常不影响 BH 退出 | 模拟 cdp 抛错 | exit code 0 |
- C05 | auto | atexit | exec 抛异常时 atexit 仍触发 | stdin 故意 raise | 占位被清 |
- C06 | auto | atexit | exec 已 close_tab 后 atexit 不报错 | close_tab() then exit | 无异常 |
- C07 | manual | atexit | KeyboardInterrupt 时 atexit 触发 | Ctrl-C BH 进程 | 占位被清(Python atexit 在 SIGINT 后跑) |
- C08 | auto | atexit | atexit 只关本 PID | STATE_FILE 有别 PID claim 的 placeholder | 不动它 |
- C09 | review | atexit | 死锁: atexit 内调 cdp 而 daemon 已停 | _close_placeholder_tabs 已 try/except | 不崩 |
- C10 | auto | atexit | 重复 bootstrap atexit 不重复注册 | safe_globals() 多调 | _atexit_registered guard 生效 |

## D. 第二 window 检测 / spawn(10)
- D01 | auto | window | pinned 仍活直接用 | 已 pin → bootstrap | 复用同 wid |
- D02 | auto | window | pinned 死掉 fallback detect | pin 一个 wid 然后手动关 window | 自动 spawn 新 wid 并 pin |
- D03 | auto | window | 系统只 1 个 window 时 spawn | 测试机仅有主 window | 自动 spawn 第二 window |
- D04 | manual | window | 系统多 window 选对 | 主 window + 用户开第二 window 自用 | detect 不挑用户那个 |
- D05 | review | window | detect 全是 user-domain 抛 RuntimeError | bootstrap catch 后 spawn | 落地正确 |
- D06 | auto | window | spawn_second_window 落地 example.com 占位 | spawn 后立刻 list tabs | 至少一个 tab URL 含 marker |
- D07 | auto | window | pin_second_window 状态持久 | bootstrap 后 STATE_FILE 含 pinned_second_window_id | 字段存在 |
- D08 | manual | window | 用户手动改 STATE_FILE pinned 为非法 wid | bootstrap 应 fallback | spawn 新 |
- D09 | review | window | spawn 焦点窃取一次 | Windows API 限制 | 接受 |
- D10 | auto | window | 多次 ensure_pinned 幂等 | 调 5 次 | 返回同 wid |

## E. tab 上限 / 累积控制(10)
- E01 | auto | cap | DEFAULT_MAX_AGENT_TABS=15 | 读常量 | 15 |
- E02 | auto | cap | 16 个 tab 时 prune 触发 | 手动构造 16 records | 删最旧的 |
- E03 | auto | cap | prune 不删本 PID 的 | 本 PID 5 个 + 别 PID 11 个 | 优先删别 PID |
- E04 | auto | cap | prune 不删未 claim 的 orphan? | 见 line 472-473 | not_mine 才考虑删 |
- E05 | auto | cap | atexit + prune 配合 | 占位 atexit 关 + prune 兜底 | 总数稳定 |
- E06 | review | cap | last_access 不刷新 stale tab | _record_access 写 mtime | 总是 now |
- E07 | auto | cap | close_tab 立即从 STATE_FILE 移除 | close_tab() 后看 state | 该 tid 不在 records |
- E08 | manual | cap | 长期跑 N 会话累积曲线 | 50 次 BH 调用 | 总数 ≤ cap |
- E09 | auto | cap | prune max_n 参数生效 | prune_agent_tabs(5) | 最多保留 5 个 |
- E10 | review | cap | DEFAULT_MAX_AGENT_TABS 全代码引用一致 | grep 看用此常量地方 | 无写死 25 残留 |

## F. 长任务 / 状态持久(8)
- F01 | manual | persist | 跨多次 BH 会话保 cookie | 第一次登录某站,关进程,第二次 goto 看是否登录态 | 仍登录 |
- F02 | auto | persist | 同进程多次 goto state 保留 | goto A → eval_js localStorage | 期望存活 |
- F03 | auto | persist | localStorage / sessionStorage | eval_js 写 LS,goto 同源,读 LS | 读得到 |
- F04 | auto | persist | iframe 不影响主页 | goto 含 iframe 站,eval_js 主 frame | 主页正常 |
- F05 | auto | persist | navigation 中 eval_js 不挂死 | goto 中途 eval_js | timeout 或正常返回 |
- F06 | auto | persist | snap 在动态页 | goto SPA + snap | DOM 已渲染 |
- F07 | manual | persist | 长 stdin 脚本(10 分钟)atexit 不提前触发 | sleep 600 + goto | 600s 后才 atexit |
- F08 | auto | persist | 重复 bootstrap 不丢 cookie | safe_globals 多调 | jar 不变 |

## G. 错误恢复 / 边界(12)
- G01 | auto | error | daemon 死掉自动重启 | kill daemon then bootstrap | ensure_daemon 重启 |
- G02 | auto | error | CDP 失联 retry | (review only — daemon level) | n/a |
- G03 | auto | error | tab 死掉时 ensure_agent_tab 重新 spawn | 关掉 agent_tab 后 ensure | 拿新 tid |
- G04 | auto | error | exec 抛异常 BH 进程仍正常 exit | stdin: raise Exception("X") | exit ≠ 0 但有错误信息 |
- G05 | auto | error | bootstrap 自身异常 fallback 提示 | mock 异常 | stderr 有 fallback 提示 |
- G06 | auto | error | goto 不存在域 | goto("https://nx-domain-xxx.invalid") | 不挂 |
- G07 | auto | error | eval_js 语法错误 | eval_js("syntax !@#") | 抛 Python 异常 / 返回 error info |
- G08 | auto | error | eval_js 运行时错 | eval_js("undefined.x") | 同 |
- G09 | auto | error | snap 0 char | snap(max_chars=0) | 空字符串 |
- G10 | auto | error | shot 不可写路径 | shot("Z:\\bad\\path") | 抛 |
- G11 | auto | error | upload 不存在文件 | upload(sel, ["nope.zip"]) | 抛 |
- G12 | auto | error | fill 不存在 selector | fill(".no-such", "x") | 抛或返回 false |

## H. 逃生口(6)
- H01 | auto | env | BH_SAFE_MODE=0 不注入 | env 设 0 + 检查 globals | goto 不在 globals |
- H02 | auto | env | BH_KEEP_PLACEHOLDERS=1 留占位 | env 设 1 + 占位 + exit + 重启 | placeholder 仍在 |
- H03 | auto | env | BH_SAFE_MODE 默认值 | 不设 | 走 safe-mode |
- H04 | auto | env | BH_SAFE_MODE=1 显式 | 设 1 | 同默认 |
- H05 | review | env | BH_SAFE_MODE=true 不识别 | code 检查只比 "0" | 任何非 "0" 都启用 — OK |
- H06 | auto | env | env 大小写敏感 | BH_safe_mode=0 | Python os.environ 大小写敏感 — 不生效,走默认 |

## I. URL / Unicode 边界(8)
- I01 | auto | url | 中文 URL | goto("https://www.baidu.com/s?wd=测试") | 不崩 |
- I02 | auto | url | emoji URL fragment | goto("https://example.com/#🎉") | 不崩 |
- I03 | auto | url | 超长 URL(2000 char query) | goto 长 URL | 不崩 |
- I04 | auto | url | URL 含 #&"' | goto 转义 URL | 不崩 |
- I05 | auto | url | bh-agent-tab marker 出现在用户访问的 URL 里 | goto("https://example.com/?bh-agent-tab=spoof") | atexit 错误地关掉?(BUG 候选) |
- I06 | auto | url | http(明文) | goto http://neverssl.com | 不崩 |
- I07 | auto | url | file:// 协议 | goto("file:///C:/...") | 取决 chrome 设置 |
- I08 | auto | url | chrome:// | goto("chrome://version") | 受 Chrome 限制 |

## J. 并发 / 重启 / 状态文件(8)
- J01 | manual | concur | 两 BH 进程同时跑 | 各拿不同 agent_tab | 是 |
- J02 | manual | concur | 两 BH 进程同时改 STATE_FILE | 无锁,丢更新? | 潜在 BUG |
- J03 | manual | restart | Chrome 关再开 | 重启后 BH 仍能 bootstrap | 是 |
- J04 | review | restart | STATE_FILE 损坏 JSON | _load_state 抛 → 默认 empty | line 96-103 |
- J05 | review | restart | STATE_FILE 不存在 | 默认 empty | line 102 |
- J06 | manual | restart | 系统重启后第一次 BH | 没 daemon → 启动 | 是 |
- J07 | auto | restart | bootstrap 在 daemon 启动慢时 | sleep delay | retry |
- J08 | review | restart | atexit 在 daemon 已退时静默 | _close_placeholder_tabs except 兜底 | 是 |

## K. 旧 helpers 兼容(8)
- K01 | auto | legacy | helpers.cdp 仍可用 | from .helpers import cdp + cdp("Target.getTargets") | 工作 |
- K02 | auto | legacy | js() 仍可用 | js("1+1") | 2 |
- K03 | auto | legacy | page_info() 仍工作 | page_info() | 返回 dict |
- K04 | auto | legacy | switch_tab/list_tabs 仍工作 | list_tabs() | 列表非空 |
- K05 | auto | legacy | new_tab/goto_url shadow 完全替换 | dir() 看名字解析到 _legacy_* | 是 |
- K06 | auto | legacy | helpers.* 全部 import 仍 OK | from .helpers import * 后能用 | 是 |
- K07 | review | legacy | SKILL.md 顶部 sample 已更新 | grep new_tab in SKILL.md | 仅在文档说明里 |
- K08 | review | legacy | agent-workspace/agent_helpers.py 是否调 new_tab | grep | 不调更好 |

## L. SKILL / memory 一致性(4)
- L01 | review | doc | SKILL.md 描述与代码一致 | 对照 bootstrap.py | 一致 |
- L02 | review | doc | memory reference 字段完整 | 检查 frontmatter | 一致 |
- L03 | review | doc | last_accessed 字段更新 | 今天日期 | 是 |
- L04 | review | doc | MEMORY.md 主索引含此 reference | 行存在 | 是 |

## 统计
- A 10 + B 10 + C 10 + D 10 + E 10 + F 8 + G 12 + H 6 + I 8 + J 8 + K 8 + L 4 = **104**(超 100)
- type 分布:auto ~70 / manual ~18 / review ~16
- 高风险 BUG 候选(必跑 + 必看):**B03/B04/B10/C07/D02/D08/G05/I05/J02**

## 跑测试约束
- 每个 auto 场景独立 BH 进程(`browser-harness <<PY ... PY`)
- runner 收集 stdout / stderr / exit code / pre & post tab snapshot
- runner 默认只跑 `type=auto`,manual / review 标 SKIP
- env: 默认 BH_SAFE_MODE=1;特定场景额外覆盖
