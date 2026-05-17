# -*- coding: utf-8 -*-
"""
============================================================
  Cloudflare Turnstile 过盾模块 - 纯CDP实现 (无需CRX插件)
============================================================
  原理: 复刻 cf-autoclick-master 插件逻辑，用 DrissionPage CDP 实现
  
  步骤:
  1. 用 CDP DOM.getFlattenedDocument(pierce=true) 找到 CF challenge iframe
  2. 注入 JS 到 iframe，hook attachShadow 捕获 shadow DOM 中的 checkbox
  3. 获取 checkbox 相对 iframe 的位置比例 (xRatio, yRatio)
  4. 用 CDP DOM.getBoxModel 获取 iframe 在页面的绝对坐标
  5. 计算 checkbox 绝对坐标 = iframe坐标 + ratio * iframe尺寸
  6. 用 CDP Input.dispatchMouseEvent 模拟真实点击
============================================================
"""
import time
import random


# ============================================================
# 注入到 CF iframe 的 JS (复刻 injected.js 逻辑)
# 在 MAIN world 运行，hook attachShadow 捕获 checkbox 位置
# ============================================================
INJECT_JS = """
(function() {
    if (window.__cf_bypass_injected) return 'already_injected';
    window.__cf_bypass_injected = true;
    window.__cf_checkbox_ratio = null;

    function getRandomInt(min, max) {
        return Math.floor(Math.random() * (max - min + 1)) + min;
    }

    // 伪造 screenX/screenY (和原插件一样)
    var screenX = getRandomInt(800, 1200);
    var screenY = getRandomInt(400, 600);
    try {
        Object.defineProperty(MouseEvent.prototype, 'screenX', { value: screenX });
        Object.defineProperty(MouseEvent.prototype, 'screenY', { value: screenY });
    } catch(e) {}

    function getNativeAttachShadow() {
        try {
            var iframe = document.createElement('iframe');
            iframe.style.display = 'none';
            document.body.appendChild(iframe);
            var native = iframe.contentWindow.Element.prototype.attachShadow;
            document.body.removeChild(iframe);
            return native;
        } catch (e) {
            return null;
        }
    }

    function runInjectionLogic() {
        try {
            var originalAttachShadow = getNativeAttachShadow();
            if (!originalAttachShadow) {
                // fallback: 直接用当前的
                originalAttachShadow = Element.prototype.attachShadow;
            }

            Element.prototype.attachShadow = function() {
                var shadowRoot = originalAttachShadow.apply(this, arguments);
                if (shadowRoot) {
                    var existingCheckbox = shadowRoot.querySelector('input[type="checkbox"]');
                    if (existingCheckbox) {
                        reportCheckbox(existingCheckbox);
                    } else {
                        var observer = new MutationObserver(function(mutations, obs) {
                            var checkbox = shadowRoot.querySelector('input[type="checkbox"]');
                            if (checkbox) {
                                reportCheckbox(checkbox);
                                obs.disconnect();
                            }
                        });
                        observer.observe(shadowRoot, { childList: true, subtree: true });
                    }
                }
                return shadowRoot;
            };
        } catch (e) {}

        // 也尝试直接查找已有的 shadow root
        setTimeout(function() { scanExistingShadowRoots(); }, 500);
        setTimeout(function() { scanExistingShadowRoots(); }, 1500);
        setTimeout(function() { scanExistingShadowRoots(); }, 3000);
    }

    function scanExistingShadowRoots() {
        if (window.__cf_checkbox_ratio) return;
        try {
            var allElements = document.querySelectorAll('*');
            for (var i = 0; i < allElements.length; i++) {
                var el = allElements[i];
                if (el.shadowRoot) {
                    var checkbox = el.shadowRoot.querySelector('input[type="checkbox"]');
                    if (checkbox) {
                        reportCheckbox(checkbox);
                        return;
                    }
                }
            }
        } catch(e) {}
    }

    function reportCheckbox(checkbox) {
        try {
            var rect = checkbox.getBoundingClientRect();
            var winW = window.innerWidth;
            var winH = window.innerHeight;
            if (winW > 0 && winH > 0 && rect.width > 0) {
                var centerX = rect.left + rect.width / 2;
                var centerY = rect.top + rect.height / 2;
                window.__cf_checkbox_ratio = {
                    xRatio: centerX / winW,
                    yRatio: centerY / winH
                };
            }
        } catch(e) {}
    }

    if (document.body) {
        runInjectionLogic();
    } else {
        var observer = new MutationObserver(function() {
            if (document.body) {
                runInjectionLogic();
                observer.disconnect();
            }
        });
        observer.observe(document.documentElement, { childList: true });
    }

    return 'injected';
})();
"""

# 从 iframe 读取 checkbox 位置比例
READ_RATIO_JS = """
(function() {
    if (window.__cf_checkbox_ratio) {
        return JSON.stringify(window.__cf_checkbox_ratio);
    }
    // fallback: 直接扫描 shadow DOM
    try {
        var allElements = document.querySelectorAll('*');
        for (var i = 0; i < allElements.length; i++) {
            var el = allElements[i];
            if (el.shadowRoot) {
                var checkbox = el.shadowRoot.querySelector('input[type="checkbox"]');
                if (checkbox) {
                    var rect = checkbox.getBoundingClientRect();
                    var winW = window.innerWidth;
                    var winH = window.innerHeight;
                    if (winW > 0 && winH > 0 && rect.width > 0) {
                        var centerX = rect.left + rect.width / 2;
                        var centerY = rect.top + rect.height / 2;
                        return JSON.stringify({
                            xRatio: centerX / winW,
                            yRatio: centerY / winH
                        });
                    }
                }
            }
        }
    } catch(e) {}
    return null;
})();
"""


def find_cf_iframe_node(page):
    """
    用 CDP 找到 Cloudflare challenge iframe 的 nodeId
    返回 (nodeId, frameId) 或 (None, None)
    """
    try:
        result = page.run_cdp('DOM.getFlattenedDocument', depth=-1, pierce=True)
        nodes = result.get('nodes', [])

        for node in nodes:
            if node.get('nodeName') == 'IFRAME':
                attrs = node.get('attributes', [])
                src = ''
                for i in range(0, len(attrs) - 1, 2):
                    if attrs[i] == 'src':
                        src = attrs[i + 1]
                        break
                if 'challenges.cloudflare.com' in src:
                    return node.get('nodeId'), node.get('frameId')
    except Exception as e:
        pass
    return None, None


def get_iframe_box(page, node_id):
    """
    获取 iframe 在页面上的绝对坐标
    返回 (x_start, y_start, width, height) 或 None
    """
    try:
        result = page.run_cdp('DOM.getBoxModel', nodeId=node_id)
        model = result.get('model', {})
        content = model.get('content', [])
        if len(content) >= 6:
            x_start = content[0]
            y_start = content[1]
            x_end = content[4]
            y_end = content[5]
            return (x_start, y_start, x_end - x_start, y_end - y_start)
    except Exception:
        pass
    return None


def inject_into_cf_iframe(page, frame_id):
    """
    在 CF iframe 的执行上下文中注入 JS
    返回注入结果
    """
    try:
        # 获取 iframe 的执行上下文
        # 方法: 用 Page.getFrameTree 或 Runtime.evaluate with contextId
        # DrissionPage 的 run_js_loaded 可能不支持 iframe
        # 用 CDP Runtime.evaluate 指定 contextId

        # 先获取所有 frame 的 execution context
        # 启用 Runtime 获取 context
        page.run_cdp('Runtime.enable')
        time.sleep(0.5)

        # 用 Page.getFrameTree 获取 frame 信息
        frame_tree = page.run_cdp('Page.getFrameTree')
        
        # 找到 CF iframe 的 frame id
        cf_frame_id = None
        
        def find_cf_frame(tree):
            frame = tree.get('frame', {})
            url = frame.get('url', '')
            if 'challenges.cloudflare.com' in url:
                return frame.get('id')
            for child in tree.get('childFrames', []):
                result = find_cf_frame(child)
                if result:
                    return result
            return None
        
        cf_frame_id = find_cf_frame(frame_tree.get('frameTree', {}))
        
        if not cf_frame_id:
            return None

        # 创建一个 isolated world 或在 frame 中执行
        # 用 Page.createIsolatedWorld 不行因为我们需要 MAIN world
        # 用 Runtime.evaluate + uniqueContextId 也不直接支持
        # 最佳方案: 用 CDP Page.addScriptToEvaluateOnNewDocument 不行因为已经加载了
        
        # 方案: 用 Runtime.evaluate 配合 contextId
        # 需要从 Runtime.executionContextCreated 事件获取 contextId
        # 但 DrissionPage 不方便监听事件
        
        # 替代方案: 用 CDP 直接在指定 frame 执行 JS
        result = page.run_cdp('Runtime.evaluate', 
                              expression=INJECT_JS,
                              contextId=None,  # 不指定就是主 frame
                              returnByValue=True)
        
        # 如果上面不行，尝试用 Page.navigate 的方式
        # 实际上最可靠的是: 找到 iframe 的 contentDocument 的 context
        
        return result
        
    except Exception as e:
        return None


def inject_via_frame_execution(page):
    """
    备选方案: 通过 Runtime API 在 CF iframe 中执行 JS
    """
    try:
        # 获取所有 execution contexts
        contexts = []
        
        # 用一个技巧: evaluate 一段 JS 来获取 iframe 并注入
        # 这个在主页面执行，通过 contentWindow 访问 iframe
        inject_from_parent_js = """
        (function() {
            var iframes = document.querySelectorAll('iframe');
            for (var i = 0; i < iframes.length; i++) {
                var src = iframes[i].src || '';
                if (src.indexOf('challenges.cloudflare.com') !== -1) {
                    try {
                        // 跨域可能失败，但 same-origin 的 turnstile widget 可以
                        var win = iframes[i].contentWindow;
                        if (win) {
                            return 'found_iframe_index_' + i;
                        }
                    } catch(e) {
                        return 'cross_origin';
                    }
                }
            }
            return 'no_iframe';
        })();
        """
        result = page.run_js(inject_from_parent_js)
        return result
    except Exception:
        return None


def get_checkbox_ratio_via_cdp(page):
    """
    纯 CDP 方案: 穿透 shadow DOM 找 checkbox，直接计算位置
    不需要注入 JS 到 iframe
    """
    try:
        # 用 DOM.getFlattenedDocument pierce=true 穿透所有 shadow DOM
        result = page.run_cdp('DOM.getFlattenedDocument', depth=-1, pierce=True)
        nodes = result.get('nodes', [])
        
        # 找 input[type="checkbox"] 在 CF iframe 内的
        # 策略: 先找 CF iframe，再在其子树中找 checkbox
        cf_iframe_node_id = None
        checkbox_node_id = None
        
        # 第一遍: 找 CF iframe
        for node in nodes:
            if node.get('nodeName') == 'IFRAME':
                attrs = node.get('attributes', [])
                src = ''
                for i in range(0, len(attrs) - 1, 2):
                    if attrs[i] == 'src':
                        src = attrs[i + 1]
                        break
                if 'challenges.cloudflare.com' in src:
                    cf_iframe_node_id = node.get('nodeId')
                    break
        
        if not cf_iframe_node_id:
            return None, None
            
        # 第二遍: 找所有 checkbox (pierce=true 已经穿透了 shadow DOM)
        for node in nodes:
            if node.get('nodeName') == 'INPUT':
                attrs = node.get('attributes', [])
                input_type = ''
                for i in range(0, len(attrs) - 1, 2):
                    if attrs[i] == 'type':
                        input_type = attrs[i + 1]
                        break
                if input_type == 'checkbox':
                    checkbox_node_id = node.get('nodeId')
                    # 可能有多个 checkbox，我们要 CF iframe 内的那个
                    # 简单策略: 取第一个 checkbox（CF 页面通常只有一个）
                    break
        
        if not checkbox_node_id:
            return None, None
        
        # 获取两者的 box model
        iframe_box = get_iframe_box(page, cf_iframe_node_id)
        
        try:
            cb_result = page.run_cdp('DOM.getBoxModel', nodeId=checkbox_node_id)
            cb_model = cb_result.get('model', {})
            cb_content = cb_model.get('content', [])
            if len(cb_content) >= 6:
                cb_x = (cb_content[0] + cb_content[4]) / 2
                cb_y = (cb_content[1] + cb_content[5]) / 2
                return (cb_x, cb_y), iframe_box
        except Exception:
            pass
        
        # 如果直接获取 checkbox box 失败，用 iframe box + 默认比例
        if iframe_box:
            # 默认 checkbox 大致在 iframe 的 (0.22, 0.5) 位置
            default_x = iframe_box[0] + iframe_box[2] * 0.22
            default_y = iframe_box[1] + iframe_box[3] * 0.5
            return (default_x, default_y), iframe_box
            
        return None, None
        
    except Exception as e:
        return None, None


def click_at_coordinates(page, x, y):
    """
    用 CDP Input.dispatchMouseEvent 在指定坐标模拟真实鼠标点击
    复刻 background.js 的 clickAtCoordinates 函数
    """
    try:
        # 添加少量随机偏移 (更真实)
        x += random.uniform(-2, 2)
        y += random.uniform(-2, 2)
        
        # mousePressed
        page.run_cdp('Input.dispatchMouseEvent',
                     type='mousePressed',
                     x=x, y=y,
                     button='left',
                     buttons=1,
                     clickCount=1)
        
        # 人类般的延迟
        time.sleep(random.uniform(0.02, 0.05))
        
        # mouseReleased
        page.run_cdp('Input.dispatchMouseEvent',
                     type='mouseReleased',
                     x=x, y=y,
                     button='left',
                     buttons=0,
                     clickCount=1)
        
        return True
    except Exception as e:
        return False


def solve_turnstile(page, max_attempts=3, poll_interval=1.0, max_wait=30):
    """
    主函数: 自动解决 Cloudflare Turnstile 验证
    
    Args:
        page: DrissionPage 的 ChromiumPage 对象
        max_attempts: 最大尝试点击次数
        poll_interval: 轮询间隔(秒)
        max_wait: 最大等待时间(秒)
    
    Returns:
        True 如果过盾成功, False 如果失败
    """
    start_time = time.time()
    
    for attempt in range(max_attempts):
        elapsed = time.time() - start_time
        if elapsed > max_wait:
            return False
        
        # 等待 CF iframe 出现
        wait_start = time.time()
        while time.time() - wait_start < 10:
            iframe_node_id, frame_id = find_cf_iframe_node(page)
            if iframe_node_id:
                break
            time.sleep(poll_interval)
        
        if not iframe_node_id:
            # 没有 CF iframe = 可能已经过了或者不需要验证
            return True
        
        # 等待一下让 checkbox 渲染
        time.sleep(random.uniform(1.5, 3.0))
        
        # 尝试获取 checkbox 坐标
        click_pos, iframe_box = get_checkbox_ratio_via_cdp(page)
        
        if click_pos:
            x, y = click_pos
            # 执行点击
            success = click_at_coordinates(page, x, y)
            if success:
                # 等待验证完成
                time.sleep(random.uniform(2.0, 4.0))
                
                # 检查是否还有 CF iframe (如果没了就是过了)
                new_node_id, _ = find_cf_iframe_node(page)
                if not new_node_id:
                    return True
                
                # iframe 还在，可能需要再等等
                time.sleep(3)
                new_node_id, _ = find_cf_iframe_node(page)
                if not new_node_id:
                    return True
        else:
            # 没找到 checkbox，用 iframe 中心偏左的默认位置
            if iframe_box:
                x = iframe_box[0] + iframe_box[2] * 0.22
                y = iframe_box[1] + iframe_box[3] * 0.5
                click_at_coordinates(page, x, y)
                time.sleep(random.uniform(3.0, 5.0))
                
                new_node_id, _ = find_cf_iframe_node(page)
                if not new_node_id:
                    return True
        
        # 重试前等待
        time.sleep(random.uniform(2.0, 4.0))
    
    return False


def is_cf_challenge_present(page):
    """检查页面是否有 CF Turnstile challenge"""
    node_id, _ = find_cf_iframe_node(page)
    return node_id is not None


def wait_and_solve_cf(page, max_wait=60, log_func=None):
    """
    等待并解决 CF 验证的便捷函数
    适合在登录流程中调用
    
    Args:
        page: DrissionPage ChromiumPage
        max_wait: 最大等待秒数
        log_func: 日志回调函数
    
    Returns:
        True = 页面可用, False = 过盾失败
    """
    def log(msg):
        if log_func:
            log_func(msg)
    
    start = time.time()
    
    # 先等页面基本加载
    time.sleep(2)
    
    while time.time() - start < max_wait:
        # 检查页面状态
        try:
            html = page.html
            html_lower = html.lower()
            
            # 已经加载完成 (有登录表单或已登录)
            if len(html) > 10000:
                return True
            
            # 被封了
            if len(html) < 3000:
                if '403 forbidden' in html_lower or '429' in html_lower:
                    log("[过盾] 检测到封禁")
                    return False
            
            # 检查是否有 CF challenge
            if is_cf_challenge_present(page):
                log("[过盾] 检测到 Turnstile, 尝试自动点击...")
                solved = solve_turnstile(page, max_attempts=3, max_wait=30)
                if solved:
                    log("[过盾] Turnstile 已通过 ✓")
                    time.sleep(2)
                    return True
                else:
                    log("[过盾] 点击后未通过, 继续等待...")
            
        except Exception as e:
            log(f"[过盾] 异常: {e}")
        
        time.sleep(2)
    
    # 超时了，检查最终状态
    try:
        if len(page.html) > 10000:
            return True
    except Exception:
        pass
    
    return False
