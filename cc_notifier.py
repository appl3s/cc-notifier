#!/usr/bin/env python3
"""
CC-notifier: Claude Code 事件通知器
用于接收 Claude Code 的 hook 事件并发送到飞书和 iOS（通过 Bark）
"""
import json
import secrets
import sys
import os
import requests
import re
from datetime import datetime
from typing import Dict, Any, Optional
from pathlib import Path
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives import padding
import base64

def aes128_cbc_encrypt(plaintext: bytes, key: bytes, iv: bytes) -> str:
    if len(key) != 16:
        raise ValueError("key 必须16字节(AES128)")
    if len(iv) != 16:
        raise ValueError("iv 必须16字节")

    padder = padding.PKCS7(128).padder()
    padded = padder.update(plaintext) + padder.finalize()

    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    enc = cipher.encryptor()
    cipher_bytes = enc.update(padded) + enc.finalize()
    return base64.b64encode(cipher_bytes).decode()

def load_config() -> Dict[str, Any]:
    """
    加载配置，优先级：环境变量 > 配置文件 > 默认值
    
    配置项：
    - feishu_webhook_url: 飞书机器人 webhook 地址
    - ios_push_url: iOS Bark 推送地址或 key
    - ios_push_enabled: 是否启用 iOS 推送
    """
    # 默认配置
    config = {
        "feishu_webhook_url": "",
        "ios_push_url": "",
        "ios_push_enabled": False
    }
    
    # 尝试从配置文件加载
    config_paths = [
        Path.home() / ".cc-notifier" / "config.json",  # 用户目录
        Path(__file__).parent / "config.json",         # 脚本所在目录
    ]
    
    for config_path in config_paths:
        if config_path.exists():
            try:
                with open(config_path, 'r', encoding='utf-8') as f:
                    file_config = json.load(f)
                    config.update(file_config)
                    break
            except Exception as e:
                print(f"警告：无法加载配置文件 {config_path}: {e}", file=sys.stderr)
    
    # 环境变量覆盖（最高优先级）
    if env_webhook := os.environ.get("FEISHU_WEBHOOK_URL"):
        config["feishu_webhook_url"] = env_webhook
    if env_ios_url := os.environ.get("IOS_PUSH_URL"):
        config["ios_push_url"] = env_ios_url
    if env_ios_enabled := os.environ.get("IOS_PUSH_ENABLED"):
        config["ios_push_enabled"] = env_ios_enabled.lower() == "true"
    
    return config


# 加载全局配置
CONFIG = load_config()
WEBHOOK_URL = CONFIG["feishu_webhook_url"]
IOS_PUSH_URL = CONFIG["ios_push_url"]
IOS_PUSH_MSG_KEY = CONFIG["ios_push_msg_key"]
IOS_PUSH_ENABLED = CONFIG["ios_push_enabled"]


def extract_last_message_from_jsonl(transcript_path: str) -> tuple[str, str]:
    """
    从 JSONL 格式的对话记录中提取最后一条消息的文本内容和 cwd 信息
    
    JSONL 文件格式：每行一个 JSON 对象，包含对话记录
    我们要找的是最后一个包含 message.content[].text 的条目
    
    返回: (last_message_text, cwd)
    """
    try:
        # 展开路径（支持 ~ 等）
        expanded_path = os.path.expanduser(transcript_path)
        if not os.path.exists(expanded_path):
            print(f"[DEBUG] JSONL 文件不存在: {expanded_path}", file=sys.stderr)
            return "", ""
        
        last_message_text = ""
        cwd = ""
        line_count = 0
        message_count = 0
        
        # 逐行读取 JSONL 文件
        with open(expanded_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                
                line_count += 1
                
                try:
                    # 解析每一行的 JSON
                    data = json.loads(line)
                    
                    # 尝试提取 cwd 信息（可能在不同位置）
                    if 'cwd' in data:
                        cwd = data.get('cwd', '')
                    elif 'workspace' in data:
                        cwd = data.get('workspace', '')
                    elif 'workingDirectory' in data:
                        cwd = data.get('workingDirectory', '')
                    elif isinstance(data.get('message'), dict) and 'cwd' in data['message']:
                        cwd = data['message'].get('cwd', '')
                    
                    # 检查是否包含 message 字段
                    if 'message' not in data or not isinstance(data['message'], dict):
                        continue
                    
                    message = data['message']
                    
                    # 检查是否包含 content 数组
                    if 'content' not in message or not isinstance(message['content'], list):
                        continue
                    
                    # 遍历 content 数组，查找 text 类型的内容
                    for content_item in message['content']:
                        if isinstance(content_item, dict) and content_item.get('type') == 'text':
                            text = content_item.get('text', '')
                            if text:
                                # 更新最后的消息文本（因为是顺序读取，最后的会覆盖之前的）
                                last_message_text = text
                                message_count += 1
                                
                except json.JSONDecodeError as e:
                    # 记录解析错误
                    print(f"[DEBUG] JSON 解析错误在第 {line_count} 行: {e}", file=sys.stderr)
                    continue
        
        print(f"[DEBUG] 读取完成: 总行数={line_count}, 消息数={message_count}, cwd={cwd}", file=sys.stderr)
        return last_message_text, cwd
        
    except Exception as e:
        print(f"读取 JSONL 文件出错：{e}", file=sys.stderr)
        return "", ""


def format_stop_message(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    格式化 Stop 事件为飞书卡片消息（使用卡片 2.0 格式）
    
    Stop 事件在 Claude Code 会话结束时触发
    包含 transcript_path 指向对话记录文件
    """
    session_id = data.get("session_id", "Unknown Session")
    stop_hook_active = data.get("stop_hook_active", False)
    transcript_path = data.get("transcript_path", "")
    
    # 从对话记录中提取最后的消息和 cwd 信息
    last_message = ""
    cwd = ""
    if transcript_path:
        last_message, cwd = extract_last_message_from_jsonl(transcript_path)
    
    # 错误关键词列表
    error_keywords = [
        "API Error:",
    ]
    
    # 检查是否包含错误关键词
    is_error = False
    if last_message:
        lower_msg = last_message.lower()
        for kw in error_keywords:
            if kw in lower_msg:
                is_error = True
                break
    
    # 构建 markdown 内容
    content = ""
    
    # 如果 stop hook 仍在激活状态，添加警告
    if stop_hook_active:
        content += "**⚠️ 警告:** Stop hook 仍在激活状态\n\n"
    
    # 显示实际内容
    if last_message:
        # 长消息截断，避免飞书消息过长
        if len(last_message) > 1000:
            content += last_message[:1000] + "...\n\n*💡 完整内容请查看终端*"
        else:
            content += last_message
    else:
        # 没有提取到消息时的后备显示
        content += f"Session {session_id[:8]}... 已结束\n\n"
        # 添加调试信息：显示 JSONL 文件路径
        if transcript_path:
            content += f"**📁 Debug - JSONL路径:**\n`{transcript_path}`"
        else:
            content += "**⚠️ Debug:** 未提供 transcript_path"
    
    # 添加项目信息（cwd）
    if cwd:
        # 获取项目名称（目录名）
        project_name = os.path.basename(cwd.rstrip('/'))
        if project_name:
            content = f"<font size=2>📂 项目: {project_name}</font>\n" + content
        else:
            content = f"<font size=2>📂 路径: {cwd}</font>\n" + content
    
    # 根据是否错误调整卡片样式
    if is_error:
        card_title = "❌ 任务异常"
        card_template = "red"
    else:
        card_title = "✅ 任务完成"
        card_template = "green"
    
    # 返回飞书卡片 2.0 格式
    return {
        "msg_type": "interactive",
        "card": {
            "schema": "2.0",  # 声明使用卡片 2.0
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": card_title
                },
                "template": card_template,  # 主题色
                "padding": "12px 12px 12px 12px"
            },
            "body": {
                "direction": "vertical",
                "padding": "12px 12px 12px 12px",
                "elements": [{
                    "tag": "markdown",
                    "content": content.strip(),
                    "text_align": "left",
                    "text_size": "normal",
                    "margin": "0px 0px 0px 0px"
                }]
            }
        }
    }


def format_notification_message(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    格式化 Notification 事件为飞书卡片消息（使用卡片 2.0 格式）
    
    Notification 事件用于各种通知场景
    """
    session_id = data.get("session_id", "Unknown Session")
    message_content = data.get("message", "")
    notification_data = data.get("notification", {})
    
    # 构建 markdown 内容
    content = ""
    
    # 优先显示 message 字段
    if message_content:
        content = f"**内容:** {message_content}\n"
    # 其次显示 notification 对象中的数据
    elif notification_data and isinstance(notification_data, dict):
        for key, value in notification_data.items():
            # 跳过一些不重要的字段
            if key not in ["session_id", "timestamp"] and value:
                content += f"**{key.title()}:** {str(value)}\n"
    
    # 如果都没有，显示基本信息
    if not content:
        content = f"Session: {session_id[:8]}..."
    
    # 返回飞书卡片 2.0 格式
    return {
        "msg_type": "interactive",
        "card": {
            "schema": "2.0",  # 声明使用卡片 2.0
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": "📢 Claude Code 通知"
                },
                "padding": "12px 12px 12px 12px"
            },
            "body": {
                "direction": "vertical",
                "padding": "12px 12px 12px 12px",
                "elements": [{
                    "tag": "markdown",
                    "content": content.strip(),
                    "text_align": "left",
                    "text_size": "normal",
                    "margin": "0px 0px 0px 0px"
                }]
            }
        }
    }


def send_ios_push_notification(title: str, message: str) -> None:
    """
    发送 iOS 推送通知（通过 Bark，使用 AES-128-CBC 加密）

    Bark 加密推送协议：
    - payload 为 JSON，包含 body/title/sound/group 等字段
    - 使用 AES-128-CBC 加密（key 在 App 端配置，不随请求发送）
    - iv 随机生成，随请求发送
    - POST ciphertext(base64) 和 iv 到 {base_url}/{deviceKey}
    """
    if not IOS_PUSH_ENABLED or not IOS_PUSH_URL:
        return

    try:
        # 清理标题中的 emoji，避免重复
        clean_title = re.sub(r'[🚨⏸️❌✅🤖🔧📢🛑🚀📝⚠️ℹ️]', '', title).strip()

        # 格式化标题和消息
        formatted_title = f"🤖 {clean_title}"
        formatted_message = f"{message}\n\n{datetime.now().strftime('%H:%M:%S')}"

        # 构造 Bark payload（JSON），所有可读字段都将被加密
        payload = {
            "title": formatted_title,
            "body": formatted_message,
            "sound": "default",
            "group": "ClaudeCode",
            "url": "paseo://",
        }
        payload_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        # 加密参数：key 在 App 端配置（16 字节），iv 随机生成（16 字节）
        key = IOS_PUSH_MSG_KEY.encode("utf-8")
        iv_str = secrets.token_hex(8)          # 16 个 hex 字符 = 16 字节
        iv = iv_str.encode("utf-8")

        # AES-128-CBC 加密 + base64
        ciphertext = aes128_cbc_encrypt(payload_bytes, key, iv)

        # 构建 Bark endpoint（IOS_PUSH_URL 已是 {base_url}/{deviceKey} 形式）
        if IOS_PUSH_URL.startswith('http'):
            url = IOS_PUSH_URL.rstrip('/')
        else:
            # 只提供了 deviceKey，构建完整 URL
            url = f"https://api.day.app/{IOS_PUSH_URL}"

        # POST ciphertext 和 iv（等价于官方 curl --data-urlencode）
        response = requests.post(url, data={
            "ciphertext": ciphertext,
            "iv": iv_str,
        }, timeout=10)

        # 检查响应
        if response.status_code != 200:
            print(f"iOS 推送失败：{response.text}", file=sys.stderr)
        else:
            result = response.json()
            if result.get('code') != 200:
                print(f"iOS 推送失败：{result.get('message', '未知错误')}", file=sys.stderr)

    except Exception as e:
        print(f"iOS 推送异常：{e}", file=sys.stderr)


def send_to_feishu(message: Dict[str, Any]) -> bool:
    """
    发送消息到飞书 webhook
    
    返回：是否发送成功
    """
    if not WEBHOOK_URL:
        print("未配置飞书 webhook URL", file=sys.stderr)
        return False
        
    try:
        response = requests.post(WEBHOOK_URL, json=message, timeout=10)
        response.raise_for_status()
        return True
    except Exception as e:
        print(f"发送到飞书失败：{e}", file=sys.stderr)
        return False


def main():
    """
    主函数：从标准输入读取 hook 数据，处理并发送通知
    """
    try:
        # 从标准输入读取 hook 数据
        hook_data = json.load(sys.stdin)

        # 获取事件类型（Claude Code 实际字段是 hook_event_name）
        event_type = hook_data.get("event_type") or hook_data.get("hook_event_name") or "Unknown"

        # 如果没有明确的事件类型，尝试从数据结构推断
        if event_type == "Unknown":
            if "stop_hook_active" in hook_data:
                event_type = "Stop"
            elif "notification" in hook_data or "message" in hook_data:
                event_type = "Notification"

        # 处理不同的事件类型
        if event_type == "Stop":
            # 处理会话结束事件
            message = format_stop_message(hook_data)

            # 发送 iOS 通知
            transcript_path = hook_data.get("transcript_path", "")
            if transcript_path:
                last_message, _ = extract_last_message_from_jsonl(transcript_path)
                if last_message:
                    # 有消息内容，发送前100字符
                    send_ios_push_notification("任务完成", last_message[:100])
                else:
                    send_ios_push_notification("任务完成", "Session 已结束")
            else:
                send_ios_push_notification("任务完成", "Session 已结束")

        elif event_type == "PreToolUse":
            tool_name = hook_data.get("tool_name", "")
            if tool_name == "AskUserQuestion":
                tool_input = hook_data.get("tool_input", {})
                questions = tool_input.get("questions", [])
                first_q = questions[0].get("question", "需要你回答") if questions else "需要你回答"
                send_ios_push_notification("需要回答", first_q[:100])
            else:
                send_ios_push_notification("PreToolUse", f"{tool_name} 即将执行")

        # FIXME: 暂时关闭 iOS 通知，减少打扰
        # elif event_type == "Notification":
        #     # 处理通知事件
        #     message = format_notification_message(hook_data)
        #     send_ios_push_notification("Claude Code 通知", "收到新通知")

        else:
            # 忽略其他事件类型（如 ToolUse 等）
            print(json.dumps({
                "success": True,
                "message": f"忽略事件类型：{event_type}"
            }))
            sys.exit(0)
        
        # 发送到飞书
        if send_to_feishu(message):
            print(json.dumps({"success": True}))
            sys.exit(0)
        else:
            print(json.dumps({
                "success": False, 
                "error": "发送到飞书失败"
            }))
            sys.exit(1)
            
    except Exception as e:
        # 全局异常处理
        print(json.dumps({
            "success": False, 
            "error": str(e)
        }))
        sys.exit(1)


if __name__ == "__main__":
    main()