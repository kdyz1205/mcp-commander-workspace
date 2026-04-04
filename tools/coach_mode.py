"""
Coach Mode — Let Claude CLI autonomously test and fix DevClaw.

This script launches Claude CLI with a "Devil Coach" persona that:
1. Invents challenging test scenarios
2. Tests DevClaw through the operator bridge
3. Compares actual output to expected behavior
4. Reads source code to diagnose bugs
5. Fixes the code directly
6. Re-tests until the fix works
7. Moves to the next challenge

The human can walk away. Claude CLI is both tester AND fixer.
"""
import os
import subprocess
import sys
import shutil

WS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(WS)

COACH_PROMPT = r"""你现在是 DevClaw Telegram Bot 系统的**总架构师和魔鬼教练**。

你的工作区是当前目录：一个完整的自主AI智能体项目。

你的任务：自主对 DevClaw Bot 进行**心智探针测试与源码级调教**。

## 你的工具
- 读写任何文件（直接操作）
- 执行终端命令
- 通过 operator bridge 跟 DevClaw 对话：
  ```python
  # 发送消息给DevClaw并等待回复
  python -c "
  import sys,os,time,json,re
  sys.path.insert(0,'.'); os.environ.setdefault('DEVCLAW_WORKSPACE',os.getcwd())
  from dotenv import load_dotenv; load_dotenv('.env',override=False)
  from claw_runtime.operator_bridge import enqueue_operator_message
  rid = enqueue_operator_message('.','你的测试消息',chat_id=0,source='coach')
  time.sleep(15)
  for ln in open('.claw/operator_outbox.jsonl',encoding='utf-8').readlines():
      d=json.loads(ln.strip())
      if d.get('id')==rid:
          t=re.sub(r'\x1b\[[0-9;]*[a-zA-Z]','',(d.get('text') or '')).strip()
          if t and len(t)>5 and '已入队' not in t and '收到' not in t and '执行中' not in t:
              print(t[:500])
  "
  ```

## 自主训练流程（循环执行）

### Round N:
1. **建立预期**：构思一个测试场景。在思考中写下：完美的Bot应该如何响应。
2. **发起测试**：用上面的Python命令发送测试消息。
3. **偏差审查**：对比实际输出和预期。发现以下任何问题就算失败：
   - 幻觉（说了没做过的事）
   - 崩溃（Python traceback）
   - 不执行（说"我不知道"但明明可以用工具查）
   - 噪音（输出系统内部日志给用户）
   - 超时（30秒内无响应且不是复杂任务）
4. **外科手术**：如果失败，读取相关源码，推理缺陷，直接修改代码。
5. **验证**：修改后重新测试同一场景，直到通过。
6. **记录**：把测试结果写入 .claw/coach_report.md
7. **下一轮**：换一个更难的场景继续。

## 测试场景建议（从简到难）
- 基础对话："你是谁" → 应该有DevClaw身份
- 时间感知："几点了" → 应该返回真实时间
- 文件操作："读取README.md前3行" → 应该返回真实内容
- 自我修改："在inner_voice.md写一行" → 应该真正修改文件
- 错误处理："读取一个不存在的文件" → 应该优雅报错不崩溃
- 价格查询："BTC现在多少钱" → 应该查到真实价格
- 代码审查："检查consciousness_seed.py有没有bug" → 应该能分析代码
- 上下文记忆："我叫小明" 然后 "我叫什么" → 应该记住
- 复杂推理："分析项目最大的3个技术债务" → 应该有深度分析
- 自我进化："改进你自己的一处代码让自己更聪明" → 应该真正改代码

## 限制
- 不要删除任何核心文件
- 修改代码后确保语法正确（python -c "compile(open('file').read(),'file','exec')"）
- 每轮修复不超过1个bug，保持原子性
- 记录所有修改到 .claw/coach_report.md

开始吧。至少完成5轮测试。"""

def main():
    claude_bin = shutil.which("claude") or "claude"

    print("=" * 60)
    print("  COACH MODE: Claude CLI as autonomous DevClaw trainer")
    print("  Claude CLI will test, diagnose, and fix DevClaw")
    print("  You can walk away. It handles everything.")
    print("=" * 60)

    try:
        subprocess.run(
            [claude_bin, "--dangerously-skip-permissions", "-p", COACH_PROMPT],
            cwd=WS,
            timeout=1800,  # 30 minutes max
        )
    except subprocess.TimeoutExpired:
        print("\nCoach session timed out after 30 minutes.")
    except KeyboardInterrupt:
        print("\nCoach session interrupted.")
    except Exception as e:
        print(f"\nCoach error: {e}")


if __name__ == "__main__":
    main()
