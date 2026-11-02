"""规划工具测试（V2/xay/cli.py）：
1. plan apply 支持整份计划格式（大中小任务、带依赖、why 说明、大白话校验），事件对且计划版本只 +1
2. 校验规则：
   - 缺 why 被拦
   - 中任务和小任务的 title 像代码（含 src/a.py、parse_order、parseOrder()、反引号、命令等）被拦并给出建议
   - 中任务包含 depts 被拦（提示部门就是中任务本身，请把它拆成单独的中任务）
   - 漏 verify / 引用不存在的 key / 循环依赖等问题整份一条不写，逐条列出大白话错误
3. --dry-run 只检查并打印树（含大任务标题和每步 why 说明），不写账本
4. 重复 apply 同一份计划转为 part.changed / task.changed，不重复加任务且版本只 +1
5. plan show 树形展示（第一行写"大任务：<对话标题>"、中任务步骤及 why、并行 2-A / 2-B、验收方式、小任务详情、超过 60 行折叠）
6. plan edit --part 挪动小任务到中任务（支持编号/key/标题）
7. 旧计划格式兼容（大白话标题校验）
8. plan open 打印画布地址或提示启动管家
9. plan part 必填 --why 存入 note；xay2 dept open --part 打印提示
"""
import io
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_cli_smoke import CliCase
from xay.core import open_db, load_state
import xay.cli


class PlanToolTests(CliCase):
    def test_plan_apply_full_plan_success_and_version(self):
        conv = self.ok('begin', '--title', '整份计划测试', conv=False)
        self.conv = conv

        plan_data = {
            "title": "更新后的对话标题",
            "groups": [
                {
                    "key": "p_api",
                    "title": "制定数据接口规范",
                    "why": "让前后端开发人员有统一的接口文档",
                    "after": [],
                    "check": "rules",
                    "tasks": [
                        {
                            "key": "t_api_spec",
                            "title": "编写接口规范与测试",
                            "agent": "codex",
                            "writes": ["api/spec.py"],
                            "card": "写接口协议并验证",
                            "verify": ["python3 -m unittest api/test_spec.py"],
                            "effort": "high"
                        }
                    ]
                },
                {
                    "key": "p_fe",
                    "title": "制作前端界面与交互",
                    "why": "让用户在浏览器看到页面并完成交互",
                    "after": ["p_api"],
                    "check": "ai:codex",
                    "check_text": "核对组件规范",
                    "tasks": [
                        {
                            "key": "t_fe_ui",
                            "title": "实现页面主要界面",
                            "to": "codex",
                            "deps": ["t_api_spec"],
                            "writes": ["web/app.js"],
                            "card": "实现界面",
                            "verify": ["npm test"]
                        }
                    ]
                },
                {
                    "key": "p_be",
                    "title": "实现后端接口服务",
                    "why": "让系统能够正确处理业务请求",
                    "after": ["p_api"],
                    "check": "lead",
                    "check_text": "核心业务逻辑必须总经理审",
                    "tasks": [
                        {
                            "key": "t_be_impl",
                            "title": "实现核心后端接口",
                            "agent": "dsh",
                            "deps": ["t_api_spec"],
                            "writes": ["api/impl.py"],
                            "card": {"text": "实现逻辑", "verify": ["pytest"]},
                            "verify": ["pytest"],
                            "readonly": False
                        }
                    ]
                }
            ]
        }

        plan_file = self.repo / 'plan.json'
        plan_file.write_text(json.dumps(plan_data, ensure_ascii=False), encoding='utf-8')

        out = self.ok('plan', 'apply', str(plan_file))
        evs = self.events()

        # 1. 对话标题更新
        titled_evs = [e for e in evs if e['type'] == 'conv.titled']
        self.assertEqual(len(titled_evs), 1)
        self.assertEqual(titled_evs[0]['data']['title'], "更新后的对话标题")

        # 2. part.added
        part_evs = [e for e in evs if e['type'] == 'part.added']
        self.assertEqual(len(part_evs), 3)
        p_api_id = next(e['subject'] for e in part_evs if e['data']['key'] == 'p_api')
        p_fe_id = next(e['subject'] for e in part_evs if e['data']['key'] == 'p_fe')
        p_be_id = next(e['subject'] for e in part_evs if e['data']['key'] == 'p_be')

        api_part_ev = next(e for e in part_evs if e['data']['key'] == 'p_api')
        self.assertEqual(api_part_ev['data']['note'], "让前后端开发人员有统一的接口文档")

        be_part_ev = next(e for e in part_evs if e['data']['key'] == 'p_be')
        self.assertEqual(be_part_ev['data']['check'], {'kind': 'lead', 'text': '核心业务逻辑必须总经理审'})
        self.assertEqual(be_part_ev['data']['after'], [p_api_id])
        self.assertEqual(be_part_ev['data']['note'], "让系统能够正确处理业务请求")

        # 3. task.added
        task_evs = [e for e in evs if e['type'] == 'task.added']
        self.assertEqual(len(task_evs), 3)

        t_spec = next(e for e in task_evs if e['data']['key'] == 't_api_spec')
        self.assertEqual(t_spec['data']['part'], p_api_id)
        self.assertEqual(t_spec['data']['card'], {
            'text': '写接口协议并验证',
            'verify': ['python3 -m unittest api/test_spec.py'],
            'effort': 'high'
        })

        t_fe = next(e for e in task_evs if e['data']['key'] == 't_fe_ui')
        self.assertEqual(t_fe['data']['deps'], [t_spec['subject']])
        self.assertEqual(t_fe['data']['part'], p_fe_id)

        # 4. 计划版本只 +1
        conn = open_db(self.db_path())
        try:
            state = load_state(conn)
            self.assertEqual(state['convs'][conv]['plan_version'], 1)
            hist = state['plan_history'].get(conv, [])
            self.assertEqual(len(hist), 1)
            self.assertEqual(hist[0]['version'], 1)
        finally:
            conn.close()

    def test_plan_apply_validation_errors(self):
        self.seed_conv('测试校验拦截')
        initial_events_count = len(self.events())

        # 1. 漏 verify
        p1 = {
            "parts": [{
                "key": "p1", "title": "搭建基础框架", "why": "搭好基础骨架", "check": "rules",
                "tasks": [{
                    "key": "t1", "title": "实现基础模块", "agent": "codex", "card": "做点事",
                    "writes": ["a.py"], "readonly": False
                }]
            }]
        }
        f1 = self.repo / 'f1.json'; f1.write_text(json.dumps(p1), encoding='utf-8')
        r1 = self.xay('plan', 'apply', str(f1))
        self.assertEqual(r1.returncode, 2)
        self.assertIn("没有验收命令（verify）", r1.stderr)
        self.assertEqual(len(self.events()), initial_events_count)

        # 2. 引用不存在的前置 key
        p2 = {
            "parts": [{
                "key": "p1", "title": "搭建基础框架", "why": "搭好基础骨架", "check": "rules", "after": ["not_exist_part"],
                "tasks": [{
                    "key": "t1", "title": "实现基础模块", "agent": "codex", "card": "卡片",
                    "readonly": True, "deps": ["not_exist_task"]
                }]
            }]
        }
        f2 = self.repo / 'f2.json'; f2.write_text(json.dumps(p2), encoding='utf-8')
        r2 = self.xay('plan', 'apply', str(f2))
        self.assertEqual(r2.returncode, 2)
        self.assertIn("引用的前置中任务「not_exist_part」不存在", r2.stderr)
        self.assertIn("引用的前置小任务「not_exist_task」不存在", r2.stderr)
        self.assertEqual(len(self.events()), initial_events_count)

        # 3. 循环依赖
        p3 = {
            "parts": [
                {
                    "key": "p1", "title": "前置模块规划", "why": "第一部分", "after": ["p2"], "check": "rules",
                    "tasks": [{
                        "key": "t1", "title": "任务一号", "agent": "codex", "card": "c1",
                        "readonly": True, "deps": ["t2"]
                    }]
                },
                {
                    "key": "p2", "title": "后置模块规划", "why": "第二部分", "after": ["p1"], "check": "rules",
                    "tasks": [{
                        "key": "t2", "title": "任务二号", "agent": "codex", "card": "c2",
                        "readonly": True, "deps": ["t1"]
                    }]
                }
            ]
        }
        f3 = self.repo / 'f3.json'; f3.write_text(json.dumps(p3), encoding='utf-8')
        r3 = self.xay('plan', 'apply', str(f3))
        self.assertEqual(r3.returncode, 2)
        self.assertIn("中任务之间存在循环依赖", r3.stderr)
        self.assertIn("小任务之间存在循环依赖", r3.stderr)
        self.assertEqual(len(self.events()), initial_events_count)

        # 4. 缺少 title、agent/to、card
        p4 = {
            "parts": [{
                "key": "p1", "title": "规划基础", "why": "搭建骨架", "check": "rules",
                "tasks": [{"key": "bad_task"}]
            }]
        }
        f4 = self.repo / 'f4.json'; f4.write_text(json.dumps(p4), encoding='utf-8')
        r4 = self.xay('plan', 'apply', str(f4))
        self.assertEqual(r4.returncode, 2)
        self.assertIn("缺少标题（title）", r4.stderr)
        self.assertIn("缺少指派对象（agent 或 to）", r4.stderr)
        self.assertIn("缺少任务卡（card）", r4.stderr)
        self.assertEqual(len(self.events()), initial_events_count)

    def test_plan_apply_warnings_not_blocking(self):
        self.seed_conv('警告不阻拦')
        plan = {
            "parts": [
                {
                    "key": "p1",
                    "title": "并行写冲突测试",
                    "why": "验证并行改动提示",
                    # 未写 check -> 警告
                    "tasks": [
                        {
                            "key": "t1", "title": "任务一", "agent": "codex", "card": "c1",
                            "writes": ["common.py"], "verify": ["echo ok"]
                        },
                        {
                            "key": "t2", "title": "任务二", "agent": "dsh", "card": "c2",
                            "writes": ["common.py"], "verify": ["echo ok"]
                        }
                    ]
                }
            ]
        }
        f = self.repo / 'warn.json'; f.write_text(json.dumps(plan), encoding='utf-8')
        r = self.xay('plan', 'apply', str(f))
        self.assertEqual(r.returncode, 0)
        self.assertIn("默认自动检查", r.stderr)
        self.assertIn("改动范围重叠", r.stderr)
        # 成功入账
        self.assertEqual(len([e for e in self.events() if e['type'] == 'task.added']), 2)

    def test_plan_apply_dry_run(self):
        self.seed_conv('干跑测试')
        initial_events_count = len(self.events())
        plan = {
            "parts": [
                {
                    "key": "p1", "title": "模块一建设", "why": "为用户提供模块一基础功能", "check": "rules",
                    "tasks": [{
                        "key": "t1", "title": "开发模块界面", "agent": "codex", "card": "卡片",
                        "writes": ["app.py"], "verify": ["pytest"]
                    }]
                }
            ]
        }
        f = self.repo / 'dry.json'; f.write_text(json.dumps(plan), encoding='utf-8')
        out = self.ok('plan', 'apply', '--dry-run', str(f))
        self.assertIn("大任务：干跑测试", out)
        self.assertIn("第 1 步：模块一建设（自动检查）", out)
        self.assertIn("为用户提供模块一基础功能", out)
        self.assertIn("t1：开发模块界面 · Codex · 计划中", out)
        # 账本里一条事件都没写
        self.assertEqual(len(self.events()), initial_events_count)

    def test_plan_apply_idempotent_duplicate_apply(self):
        conv = self.ok('begin', '--title', '重复应用测试', conv=False)
        self.conv = conv

        plan = {
            "parts": [
                {
                    "key": "p1", "title": "第一版中任务", "why": "第一版交付价值", "check": "rules",
                    "tasks": [{
                        "key": "t1", "title": "第一版小任务", "agent": "codex", "card": "卡片",
                        "writes": ["app.py"], "verify": ["pytest"]
                    }]
                }
            ]
        }
        f = self.repo / 'idem.json'; f.write_text(json.dumps(plan), encoding='utf-8')

        # 第一次 apply -> part.added, task.added
        self.ok('plan', 'apply', str(f))
        conn = open_db(self.db_path())
        try:
            s1 = load_state(conn)
            self.assertEqual(s1['convs'][conv]['plan_version'], 1)
            self.assertEqual(len(s1['parts']), 1)
            self.assertEqual(len(s1['tasks']), 1)
        finally:
            conn.close()

        # 第二次 apply 修改标题与任务标题
        plan['parts'][0]['title'] = "第二版中任务"
        plan['parts'][0]['why'] = "第二版新交付价值"
        plan['parts'][0]['tasks'][0]['title'] = "第二版小任务"
        f.write_text(json.dumps(plan), encoding='utf-8')

        self.ok('plan', 'apply', str(f))
        conn = open_db(self.db_path())
        try:
            s2 = load_state(conn)
            # 版本 +1 变成 2
            self.assertEqual(s2['convs'][conv]['plan_version'], 2)
            # 数量没有重复增加
            self.assertEqual(len(s2['parts']), 1)
            self.assertEqual(len(s2['tasks']), 1)
            # 内容更新
            pt = next(iter(s2['parts'].values()))
            self.assertEqual(pt['title'], "第二版中任务")
            self.assertEqual(pt['note'], "第二版新交付价值")
            tk = next(iter(s2['tasks'].values()))
            self.assertEqual(tk['title'], "第二版小任务")
        finally:
            conn.close()

        evs = self.events()
        self.assertEqual(len([e for e in evs if e['type'] == 'part.added']), 1)
        self.assertEqual(len([e for e in evs if e['type'] == 'part.changed']), 1)
        self.assertEqual(len([e for e in evs if e['type'] == 'task.added']), 1)
        self.assertEqual(len([e for e in evs if e['type'] == 'task.changed']), 1)

    def test_plan_show_tree_formatting(self):
        self.seed_conv('任务树展示')
        plan = {
            "parts": [
                {
                    "key": "step1", "title": "前期架构设计", "why": "梳理好模块骨架", "check": "rules",
                    "tasks": [{
                        "key": "t_arch", "title": "绘制架构流程图", "agent": "codex", "card": "卡片",
                        "readonly": True
                    }]
                },
                {
                    "key": "step2_a", "title": "前端界面开发", "why": "呈现可视操作界面", "after": ["step1"], "check": "ai:codex",
                    "tasks": [{
                        "key": "t_ui", "title": "实现基础组件", "agent": "codex",
                        "card": "UI卡片", "writes": ["ui.vue"], "verify": ["vitest"]
                    }]
                },
                {
                    "key": "step2_b", "title": "后端接口开发", "why": "提供数据查询与存取能力", "after": ["step1"], "check": "lead",
                    "tasks": [{
                        "key": "t_api", "title": "实现服务端接口", "agent": "dsh",
                        "card": "API卡片", "writes": ["api.py"], "verify": ["pytest"]
                    }]
                }
            ]
        }
        f = self.repo / 'tree.json'; f.write_text(json.dumps(plan), encoding='utf-8')
        self.ok('plan', 'apply', str(f))

        out = self.ok('plan', 'show')
        lines = out.strip().splitlines()

        # 第一行写"大任务：<对话标题>"
        self.assertEqual(lines[0], "大任务：任务树展示")
        # 检查步骤格式及并行标号 2-A / 2-B
        self.assertTrue(any("第 1 步：前期架构设计（自动检查）" in ln for ln in lines))
        self.assertTrue(any("梳理好模块骨架" in ln for ln in lines))
        self.assertTrue(any("第 2-A 步：前端界面开发（Codex 审查）" in ln for ln in lines))
        self.assertTrue(any("呈现可视操作界面" in ln for ln in lines))
        self.assertTrue(any("第 2-B 步：后端接口开发（负责人亲自审）" in ln for ln in lines))
        self.assertTrue(any("提供数据查询与存取能力" in ln for ln in lines))
        # 任务行格式
        self.assertTrue(any("（t_ui）：实现基础组件 · Codex · 计划中" in ln for ln in lines))

    def test_plan_show_folding_over_60_lines(self):
        self.seed_conv('长计划折叠')
        tasks = []
        for i in range(70):
            tasks.append({
                "key": f"task_{i}", "title": f"第{i}件小任务", "agent": "codex", "card": "卡片", "readonly": True
            })
        plan = {
            "parts": [{
                "key": "huge_part", "title": "超长工作清单", "why": "批处理小任务集", "check": "rules",
                "tasks": tasks
            }]
        }
        f = self.repo / 'huge.json'; f.write_text(json.dumps(plan), encoding='utf-8')
        self.ok('plan', 'apply', str(f))

        out = self.ok('plan', 'show')
        lines = out.strip().splitlines()
        self.assertLessEqual(len(lines), 60)
        self.assertTrue(lines[-1].startswith("…还有"))

    def test_plan_edit_part_option(self):
        self.seed_conv('任务挪动')
        pt1 = self.ok('plan', 'part', '前期阶段', '--why', '第一阶段目标', '--key', 'p1')
        pt2 = self.ok('plan', 'part', '后期阶段', '--why', '第二阶段目标', '--key', 'p2')

        tk = self.ok('plan', 'add', '待挪动任务', '--key', 'move_tk', '--part', 'p1')

        # 按 key 挪动到阶段二
        self.ok('plan', 'edit', 'move_tk', '--part', 'p2')
        evs = self.events()
        ch1 = next(e for e in reversed(evs) if e['type'] == 'task.changed' and e['subject'] == tk)
        self.assertEqual(ch1['data']['fields']['part'], pt2)

        # 按标题挪动回阶段一
        self.ok('plan', 'edit', tk, '--part', '前期阶段')
        evs = self.events()
        ch2 = next(e for e in reversed(evs) if e['type'] == 'task.changed' and e['subject'] == tk)
        self.assertEqual(ch2['data']['fields']['part'], pt1)

    def test_plan_apply_legacy_format(self):
        self.seed_conv('旧格式兼容')
        legacy_plan = [
            {"title": "旧任务一", "key": "old1"},
            {"title": "旧任务二", "key": "old2", "deps": ["old1"]}
        ]
        f = self.repo / 'legacy.json'; f.write_text(json.dumps(legacy_plan), encoding='utf-8')
        out = self.ok('plan', 'apply', str(f))
        added = [e for e in self.events() if e['type'] == 'task.added']
        self.assertEqual(len(added), 2)
        self.assertEqual(added[0]['data']['title'], '旧任务一')
        self.assertEqual(added[1]['data']['title'], '旧任务二')

    def test_plan_open(self):
        self.seed_conv('打开画布')
        # 1. 真实子进程：管家未运行，提示先 office start
        r = self.xay('plan', 'open')
        self.assertEqual(r.returncode, 2)
        self.assertIn("office start", r.stderr)

        # 2. 模拟管家运行
        saved_env = dict(self.env(conv=True))
        with patch.dict('os.environ', saved_env, clear=True):
            with patch('xay.office.daemon.running_info', return_value=(12345, 7879, 100)):
                buf = io.StringIO()
                with patch('sys.stdout', buf):
                    rc = xay.cli.main(['--conv', self.conv, 'plan', 'open'])
                    self.assertEqual(rc, 0)
                expected = f"http://127.0.0.1:7879/org?conv={self.conv}&canvas=1"
                self.assertEqual(buf.getvalue().strip(), expected)

    def test_plan_plain_and_why_validation(self):
        """测试大白话把关与 why 必填规则（protocol §1.30）。"""
        self.seed_conv('大白话与WHY测试')

        # 1. plan part 缺 --why 被拦
        r_part_no_why = self.xay('plan', 'part', '定接口')
        self.assertEqual(r_part_no_why.returncode, 2)
        self.assertIn("why", r_part_no_why.stderr.lower())

        # 2. plan part 传 --why 成功，并写入 note
        pt = self.ok('plan', 'part', '确定接口设计', '--why', '让前后端对齐数据接口', '--key', 'api_pt')
        evs = self.events()
        part_ev = next(e for e in evs if e['type'] == 'part.added' and e['subject'] == pt)
        self.assertEqual(part_ev['data']['note'], '让前后端对齐数据接口')

        # 3. 计划文件缺 why 被拦
        plan_missing_why = {
            "parts": [
                {
                    "title": "搭建核心架构",
                    "tasks": [{"title": "编写架构核心代码", "agent": "codex", "card": "卡片", "readonly": True}]
                }
            ]
        }
        f_no_why = self.repo / 'no_why.json'; f_no_why.write_text(json.dumps(plan_missing_why), encoding='utf-8')
        r_no_why = self.xay('plan', 'apply', str(f_no_why))
        self.assertEqual(r_no_why.returncode, 2)
        self.assertIn("缺少 why", r_no_why.stderr)

        # 4. 标题像代码（含 src/a.py、parse_order、parseOrder()、反引号）都被拦并给出建议
        bad_titles = [
            ("实现 src/a.py 逻辑", "src/a.py"),
            ("调用 parse_order 函数", "parse_order"),
            ("实现 parseOrder() 方法", "parseOrder()"),
            ("处理 `config` 参数", "`"),
        ]
        for bad_title, token in bad_titles:
            bad_plan = {
                "parts": [
                    {
                        "title": "基础组开发",
                        "why": "提供基础功能",
                        "tasks": [{
                            "title": bad_title,
                            "agent": "codex",
                            "card": "卡片",
                            "readonly": True
                        }]
                    }
                ]
            }
            f_bad = self.repo / 'bad.json'; f_bad.write_text(json.dumps(bad_plan), encoding='utf-8')
            r_bad = self.xay('plan', 'apply', str(f_bad))
            self.assertEqual(r_bad.returncode, 2, f"标题「{bad_title}」应被拦截")
            self.assertIn("标题像代码", r_bad.stderr)
            self.assertIn("建议改成说它要达成什么，比如『让下单时价格算对』", r_bad.stderr)
            self.assertIn(token, r_bad.stderr)

        # 5. 中任务标题像代码同样被拦并给出建议
        bad_part_plan = {
            "parts": [
                {
                    "title": "修改 src/a.py 文件",
                    "why": "修改代码",
                    "tasks": [{"title": "处理逻辑", "agent": "codex", "card": "卡片", "readonly": True}]
                }
            ]
        }
        f_bad_part = self.repo / 'bad_part.json'; f_bad_part.write_text(json.dumps(bad_part_plan), encoding='utf-8')
        r_bad_part = self.xay('plan', 'apply', str(f_bad_part))
        self.assertEqual(r_bad_part.returncode, 2)
        self.assertIn("第 1 个中任务「修改 src/a.py 文件」的标题像代码", r_bad_part.stderr)
        self.assertIn("建议改成说它要达成什么，比如『让下单时价格算对』", r_bad_part.stderr)

        # 6. 中任务里不再支持 depts
        dept_plan = {
            "parts": [
                {
                    "title": "前端开发组",
                    "why": "交付前端页面",
                    "depts": [{"key": "web", "name": "页面开发部"}],
                    "tasks": [{"title": "开发主页", "agent": "codex", "card": "卡片", "readonly": True}]
                }
            ]
        }
        f_dept = self.repo / 'dept.json'; f_dept.write_text(json.dumps(dept_plan), encoding='utf-8')
        r_dept = self.xay('plan', 'apply', str(f_dept))
        self.assertEqual(r_dept.returncode, 2)
        self.assertIn("部门就是中任务本身，请把它拆成单独的中任务", r_dept.stderr)

        # 7. xay2 dept open --part 打印提示
        r_dept_open = self.xay('dept', 'open', 'web', '--name', '页面组', '--part', 'api_pt')
        self.assertEqual(r_dept_open.returncode, 0)
        self.assertIn("建议直接用 xay2 plan part 开中任务", r_dept_open.stderr)

        # 8. 大白话标题全部通过
        good_plan = {
            "groups": [
                {
                    "title": "下单金额结算",
                    "why": "让下单时价格算对",
                    "tasks": [
                        {
                            "title": "核算满减折扣",
                            "agent": "codex",
                            "card": "计算折扣并核对",
                            "readonly": True
                        }
                    ]
                }
            ]
        }
        f_good = self.repo / 'good.json'; f_good.write_text(json.dumps(good_plan), encoding='utf-8')
        r_good = self.xay('plan', 'apply', str(f_good))
        self.assertEqual(r_good.returncode, 0)


if __name__ == '__main__':
    unittest.main()
