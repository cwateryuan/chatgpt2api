"use client";

import { AlertTriangle, LoaderCircle, Plus, Play, RotateCcw, Save, Square, Trash2, UserPlus } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";

import { useSettingsStore } from "../../settings/store";

export function RegisterCard() {
  const config = useSettingsStore((state) => state.registerConfig);
  const isLoading = useSettingsStore((state) => state.isLoadingRegister);
  const isSaving = useSettingsStore((state) => state.isSavingRegister);
  const setProxy = useSettingsStore((state) => state.setRegisterProxy);
  const setTotal = useSettingsStore((state) => state.setRegisterTotal);
  const setThreads = useSettingsStore((state) => state.setRegisterThreads);
  const setEngine = useSettingsStore((state) => state.setRegisterEngine);
  const setBrowserTokenMode = useSettingsStore((state) => state.setRegisterBrowserTokenMode);
  const setMode = useSettingsStore((state) => state.setRegisterMode);
  const setTargetQuota = useSettingsStore((state) => state.setRegisterTargetQuota);
  const setTargetAvailable = useSettingsStore((state) => state.setRegisterTargetAvailable);
  const setCheckInterval = useSettingsStore((state) => state.setRegisterCheckInterval);
  const setMailField = useSettingsStore((state) => state.setRegisterMailField);
  const setMailAutoDisable = useSettingsStore((state) => state.setRegisterMailAutoDisable);
  const addProvider = useSettingsStore((state) => state.addRegisterProvider);
  const updateProvider = useSettingsStore((state) => state.updateRegisterProvider);
  const deleteProvider = useSettingsStore((state) => state.deleteRegisterProvider);
  const save = useSettingsStore((state) => state.saveRegister);
  const toggle = useSettingsStore((state) => state.toggleRegister);
  const reset = useSettingsStore((state) => state.resetRegister);
  const resetOutlookPool = useSettingsStore((state) => state.resetOutlookPool);
  const resetMailHealth = useSettingsStore((state) => state.resetRegisterMailHealth);

  if (isLoading) {
    return (
      <div className="flex items-center justify-center rounded-xl border border-stone-200 bg-white/80 p-10">
        <LoaderCircle className="size-5 animate-spin text-stone-400" />
      </div>
    );
  }

  if (!config) return null;

  const mail = config.mail || { request_timeout: 30, wait_timeout: 120, wait_interval: 3, auto_disable: true, failure_threshold: 10, providers: [] };
  const stats = config.stats || { success: 0, fail: 0, done: 0, running: 0, threads: config.threads };
  const providers = mail.providers || [];
  const logs = config.logs || [];
  const isEnabled = Boolean(config.enabled);
  const isStopping = !isEnabled && Number(stats.running || 0) > 0;
  const isRuntimeBusy = isEnabled || isStopping;
  const browserUnavailable = config.engine === "browser" && !config.browser_available;
  const updateProviderType = (index: number, type: string) => {
    updateProvider(index, {
      id: "",
      health: {},
      type,
      enable: true,
      ...(type === "cloudmail_gen" ? { api_base: "", admin_email: "", admin_password: "", domain: [], subdomain: [], email_prefix: "" } : {}),
      ...(type === "cloudflare_temp_email" ? { api_base: "", admin_password: "", domain: [] } : {}),
      ...(type === "tempmail_lol" ? { api_key: "", domain: [] } : {}),
      ...(type === "moemail" ? { api_base: "", api_key: "", domain: [] } : {}),
      ...(type === "inbucket" ? { api_base: "", domain: [], random_subdomain: true } : {}),
      ...(type === "duckmail" ? { api_key: "", default_domain: "duckmail.sbs" } : {}),
      ...(type === "gptmail" ? { api_key: "", default_domain: "" } : {}),
      ...(type === "yyds_mail" ? { api_base: "https://maliapi.215.im/v1", api_key: "", domain: [], subdomain: "", wildcard: false } : {}),
      ...(type === "ddg_mail" ? { ddg_token: "", cf_inbox_jwt: "", cf_domain: [], admin_password: "" } : {}),
      ...(type === "outlook_token" ? { mailboxes: "", mode: "graph", imap_host: "outlook.office365.com", message_limit: 10, alias_enabled: false, alias_per_email: 5, alias_prefix: "c2api", alias_include_original: true } : {}),
      ...(type === "mailpit" ? { api_url: "", domain: [], domain_mode: "round_robin" } : {}),
    });
  };

  return (
    <div className="grid h-[calc(100vh-132px)] min-h-[640px] items-stretch gap-0 overflow-hidden rounded-xl border border-stone-200 bg-white/70 xl:grid-cols-2">
      <section className="min-w-0 space-y-4 overflow-y-auto border-b border-stone-200 p-4 xl:border-r xl:border-b-0">
          <div className="flex items-start justify-between gap-3">
            <div className="flex items-center gap-3">
              <div className="flex size-9 items-center justify-center rounded-md bg-stone-100">
                <UserPlus className="size-5 text-stone-600" />
              </div>
              <div>
                <h2 className="text-lg font-semibold tracking-tight">注册配置</h2>
              </div>
            </div>
            <Button className="h-9 rounded-xl bg-stone-950 px-4 text-white hover:bg-stone-800" onClick={() => void save()} disabled={isSaving || isRuntimeBusy}>
              {isSaving ? <LoaderCircle className="size-4 animate-spin" /> : <Save className="size-4" />}
              保存配置
            </Button>
          </div>

          <div className="flex items-start gap-2 rounded-xl border border-sky-200 bg-sky-50 px-3 py-2 text-xs leading-5 text-sky-800">
            <AlertTriangle className="mt-0.5 size-4 shrink-0" />
            <span>如果注册日志出现 Cloudflare 拦截，可在设置页启用 FlareSolverr 清障；相关 Docker 容器需要先启动。</span>
          </div>

          <div className="space-y-2">
            <label className="text-sm text-stone-700">注册引擎</label>
            <div className="grid h-10 grid-cols-2 border border-stone-200 bg-stone-50 p-1">
              <button type="button" className={`text-sm transition ${config.engine === "http" ? "bg-white font-medium text-stone-900 shadow-sm" : "text-stone-500"}`} onClick={() => setEngine("http")} disabled={isRuntimeBusy}>
                HTTP
              </button>
              <button type="button" className={`text-sm transition disabled:cursor-not-allowed disabled:opacity-40 ${config.engine === "browser" ? "bg-white font-medium text-stone-900 shadow-sm" : "text-stone-500"}`} onClick={() => setEngine("browser")} disabled={isRuntimeBusy || !config.browser_available} title={config.browser_available ? "Chromium" : config.browser_error || "浏览器运行环境不可用"}>
                浏览器
              </button>
            </div>
            {config.engine === "browser" && config.browser_available ? <p className="text-xs text-stone-500">Chromium {config.browser_version || "available"}，每个并发任务使用独立浏览器实例。</p> : null}
            {browserUnavailable ? <p className="text-xs text-rose-600">浏览器运行环境不可用：{config.browser_error || "Chromium 未安装"}</p> : null}
          </div>

          {config.engine === "browser" ? (
            <div className="space-y-2">
              <label className="text-sm text-stone-700">入池凭据</label>
              <div className="grid h-10 grid-cols-2 border border-stone-200 bg-stone-50 p-1">
                <button type="button" className={`text-sm transition ${config.browser_token_mode === "session" ? "bg-white font-medium text-stone-900 shadow-sm" : "text-stone-500"}`} onClick={() => setBrowserTokenMode("session")} disabled={isRuntimeBusy}>
                  Session Token
                </button>
                <button type="button" className={`text-sm transition ${config.browser_token_mode === "oauth" ? "bg-white font-medium text-stone-900 shadow-sm" : "text-stone-500"}`} onClick={() => setBrowserTokenMode("oauth")} disabled={isRuntimeBusy}>
                  OAuth Token
                </button>
              </div>
            </div>
          ) : null}

          <div className="grid gap-4 md:grid-cols-3">
            <div className="space-y-2">
              <label className="text-sm text-stone-700">注册模式</label>
              <Select value={config.mode || "total"} onValueChange={(value) => setMode(value as "total" | "quota" | "available")} disabled={isRuntimeBusy}>
                <SelectTrigger className="h-10 rounded-xl border-stone-200 bg-white">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="total">注册总数</SelectItem>
                  <SelectItem value="quota">号池剩余额度</SelectItem>
                  <SelectItem value="available">可用账号数量</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-2">
              <label className="text-sm text-stone-700">注册总数</label>
              <Input value={String(config.total)} onChange={(event) => setTotal(event.target.value)} className="h-10 rounded-xl border-stone-200 bg-white" disabled={isRuntimeBusy || config.mode !== "total"} />
            </div>
            <div className="space-y-2">
              <label className="text-sm text-stone-700">{config.engine === "browser" ? "浏览器并发数" : "线程数"}</label>
              <Input value={String(config.threads)} onChange={(event) => setThreads(event.target.value)} className="h-10 rounded-xl border-stone-200 bg-white" disabled={isRuntimeBusy} />
            </div>
            <div className="space-y-2 md:col-span-3">
              <label className="text-sm text-stone-700">{config.engine === "browser" ? "注册代理（每行一个，仅注册）" : "注册代理（每行一个）"}</label>
              <Textarea
                value={config.proxy}
                onChange={(event) => setProxy(event.target.value)}
                placeholder={config.engine === "browser" ? "host:port:username:password\nhost:port:username:password" : "http://127.0.0.1:7890\nhttp://127.0.0.1:7891"}
                className="min-h-24 resize-y rounded-xl border-stone-200 bg-white font-mono text-xs"
                disabled={isRuntimeBusy}
              />
            </div>
            <div className="space-y-2">
              <label className="text-sm text-stone-700">目标剩余额度</label>
              <Input value={String(config.target_quota || "")} onChange={(event) => setTargetQuota(event.target.value)} className="h-10 rounded-xl border-stone-200 bg-white" disabled={isRuntimeBusy || config.mode !== "quota"} />
            </div>
            <div className="space-y-2">
              <label className="text-sm text-stone-700">目标可用账号</label>
              <Input value={String(config.target_available || "")} onChange={(event) => setTargetAvailable(event.target.value)} className="h-10 rounded-xl border-stone-200 bg-white" disabled={isRuntimeBusy || config.mode !== "available"} />
            </div>
            <div className="space-y-2">
              <label className="text-sm text-stone-700">检查间隔（秒）</label>
              <Input value={String(config.check_interval || "")} onChange={(event) => setCheckInterval(event.target.value)} className="h-10 rounded-xl border-stone-200 bg-white" disabled={isRuntimeBusy || config.mode === "total"} />
            </div>
          </div>

          <div className="space-y-3 border-t border-stone-200 pt-3">
            <div className="flex items-center justify-between gap-3">
              <div>
                <h3 className="text-sm font-semibold text-stone-800">邮箱配置</h3>
                <p className="mt-1 text-xs text-stone-500">数字越小优先级越高，仅使用当前最高优先级的可用渠道。</p>
              </div>
              <Button type="button" variant="outline" className="h-9 rounded-xl border-stone-200 bg-white px-3 text-stone-700" onClick={addProvider} disabled={isRuntimeBusy}>
                <Plus className="size-4" />
                添加
              </Button>
            </div>

            <div className="grid gap-4 md:grid-cols-3">
              <div className="space-y-2">
                <label className="text-sm text-stone-700">请求超时</label>
                <Input value={String(mail.request_timeout || "")} onChange={(event) => setMailField("request_timeout", event.target.value)} className="h-10 rounded-xl border-stone-200 bg-white" disabled={isRuntimeBusy} />
              </div>
              <div className="space-y-2">
                <label className="text-sm text-stone-700">等待验证码超时</label>
                <Input value={String(mail.wait_timeout || "")} onChange={(event) => setMailField("wait_timeout", event.target.value)} className="h-10 rounded-xl border-stone-200 bg-white" disabled={isRuntimeBusy} />
              </div>
              <div className="space-y-2">
                <label className="text-sm text-stone-700">轮询间隔</label>
                <Input value={String(mail.wait_interval || "")} onChange={(event) => setMailField("wait_interval", event.target.value)} className="h-10 rounded-xl border-stone-200 bg-white" disabled={isRuntimeBusy} />
              </div>
              <label className="flex h-10 items-center gap-3 self-end text-sm text-stone-700">
                <Checkbox checked={Boolean(mail.auto_disable)} onCheckedChange={(checked) => setMailAutoDisable(Boolean(checked))} disabled={isRuntimeBusy} />
                连续失败自动禁用
              </label>
              <div className="space-y-2">
                <label className="text-sm text-stone-700">连续失败阈值</label>
                <Input type="number" min={1} value={String(mail.failure_threshold || 10)} onChange={(event) => setMailField("failure_threshold", event.target.value)} className="h-10 rounded-xl border-stone-200 bg-white" disabled={isRuntimeBusy || !mail.auto_disable} />
              </div>
              <Button type="button" variant="outline" className="h-10 self-end rounded-xl border-stone-200 bg-white px-3 text-stone-700" onClick={() => void resetMailHealth()} disabled={isSaving}>
                <RotateCcw className="size-4" />
                恢复全部渠道
              </Button>
            </div>

            <div className="space-y-3">
              {providers.map((provider, index) => {
                const type = String(provider.type || "tempmail_lol");
                const domains = Array.isArray(provider.domain) ? provider.domain.map(String).join("\n") : "";
                const subdomains = Array.isArray(provider.subdomain) ? provider.subdomain.map(String).join("\n") : "";
                const health = (provider.health || {}) as Record<string, unknown>;
                const healthDomains = Array.isArray(health.domains) ? health.domains as Array<Record<string, unknown>> : [];
                const providerId = String(provider.id || "");
                const disabledByHealth = Boolean(health.disabled);
                const latchedDisabled = Boolean(health.latched_disabled);
                return (
                  <div key={index} className="space-y-3 border-t border-stone-200 pt-3 first:border-t-0 first:pt-0">
                    <div className="flex items-center justify-between gap-3">
                      <label className="flex items-center gap-3 text-sm text-stone-700">
                        <Checkbox checked={Boolean(provider.enable)} onCheckedChange={(checked) => updateProvider(index, { enable: Boolean(checked) })} disabled={isRuntimeBusy} />
                        启用
                      </label>
                      {!provider.enable ? <Badge variant="secondary" className="rounded-md">手动停用</Badge> : type === "outlook_token" && Boolean(health.exhausted) ? <Badge variant="warning" className="rounded-md">邮箱池已耗尽</Badge> : disabledByHealth ? <Badge variant="danger" className="rounded-md">已自动禁用</Badge> : latchedDisabled ? <Badge variant="warning" className="rounded-md">禁用状态已暂停</Badge> : Number(health.consecutive_failures || 0) > 0 ? <Badge variant="warning" className="rounded-md">连续失败 {Number(health.consecutive_failures)}</Badge> : <Badge variant="success" className="rounded-md">可用</Badge>}
                      <button type="button" className="rounded-lg p-2 text-stone-400 transition hover:bg-rose-50 hover:text-rose-500 disabled:opacity-50" onClick={() => deleteProvider(index)} disabled={isRuntimeBusy || providers.length <= 1} title="删除 provider">
                        <Trash2 className="size-4" />
                      </button>
                    </div>

                    <div className="grid gap-4 md:grid-cols-2">
                      <div className="space-y-2">
                        <label className="text-sm text-stone-700">类型</label>
                        <Select value={type} onValueChange={(value) => updateProviderType(index, value)} disabled={isRuntimeBusy}>
                          <SelectTrigger className="h-10 rounded-xl border-stone-200 bg-white">
                            <SelectValue />
                          </SelectTrigger>
                          <SelectContent>
                            <SelectItem value="cloudmail_gen">cloudmail_gen</SelectItem>
                            <SelectItem value="cloudflare_temp_email">cloudflare_temp_email</SelectItem>
                            <SelectItem value="tempmail_lol">tempmail_lol</SelectItem>
                            <SelectItem value="moemail">moemail</SelectItem>
                            <SelectItem value="inbucket">inbucket_mail</SelectItem>
                            <SelectItem value="duckmail">duckmail</SelectItem>
                            <SelectItem value="gptmail">gptmail(未测试)</SelectItem>
                            <SelectItem value="yyds_mail">yyds_mail</SelectItem>
                            <SelectItem value="ddg_mail">ddg_mail (DDG邮箱+CF中转)</SelectItem>
                            <SelectItem value="outlook_token">outlook_token (Outlook/Hotmail 邮箱池)</SelectItem>
                            <SelectItem value="mailpit">mailpit (本地 Mailpit)</SelectItem>
                          </SelectContent>
                        </Select>
                      </div>
                      <div className="space-y-2">
                        <label className="text-sm text-stone-700">优先级</label>
                        <Input type="number" min={1} value={String(provider.priority || index + 1)} onChange={(event) => updateProvider(index, { priority: Math.max(1, Number(event.target.value) || 1) })} className="h-10 rounded-xl border-stone-200 bg-white" disabled={isRuntimeBusy} />
                      </div>
                      {type === "cloudmail_gen" || type === "cloudflare_temp_email" || type === "moemail" || type === "inbucket" || type === "yyds_mail" || type === "ddg_mail" ? (
                        <>
                          <div className="space-y-2">
                            <label className="text-sm text-stone-700">{type === "cloudmail_gen" ? "CloudMail URL" : "API Base"}</label>
                            <Input value={String(provider.api_base || "")} onChange={(event) => updateProvider(index, { api_base: event.target.value })} className="h-10 rounded-xl border-stone-200 bg-white" disabled={isRuntimeBusy} />
                          </div>
                          {type === "cloudmail_gen" ? (
                            <>
                              <div className="space-y-2">
                                <label className="text-sm text-stone-700">管理员邮箱</label>
                                <Input value={String(provider.admin_email || "")} onChange={(event) => updateProvider(index, { admin_email: event.target.value })} className="h-10 rounded-xl border-stone-200 bg-white" disabled={isRuntimeBusy} />
                              </div>
                              <div className="space-y-2">
                                <label className="text-sm text-stone-700">管理员密码</label>
                                <Input value={String(provider.admin_password || "")} onChange={(event) => updateProvider(index, { admin_password: event.target.value })} className="h-10 rounded-xl border-stone-200 bg-white" disabled={isRuntimeBusy} />
                              </div>
                            </>
                          ) : null}
                          {type === "cloudflare_temp_email" || type === "ddg_mail" ? (
                            <div className="space-y-2">
                              <label className="text-sm text-stone-700">Admin Password</label>
                              <Input value={String(provider.admin_password || "")} onChange={(event) => updateProvider(index, { admin_password: event.target.value })} className="h-10 rounded-xl border-stone-200 bg-white" disabled={isRuntimeBusy} />
                            </div>
                          ) : null}
                        </>
                      ) : null}
                      {type === "mailpit" ? (
                        <>
                          <div className="space-y-2">
                            <label className="text-sm text-stone-700">Messages API URL</label>
                            <Input value={String(provider.api_url || "")} onChange={(event) => updateProvider(index, { api_url: event.target.value })} className="h-10 rounded-xl border-stone-200 bg-white" disabled={isRuntimeBusy} placeholder="http://mailpit:8025" />
                          </div>
                          <div className="space-y-2">
                            <label className="text-sm text-stone-700">邮箱域名</label>
                            <Textarea value={domains} onChange={(event) => updateProvider(index, { domain: event.target.value.split(/[,\n]/).map((item) => item.trim()).filter(Boolean) })} className="min-h-20 rounded-xl border-stone-200 bg-white font-mono text-xs" disabled={isRuntimeBusy} placeholder={"a.example.com, b.example.com\n或每行一个域名"} />
                          </div>
                          <div className="space-y-2">
                            <label className="text-sm text-stone-700">域名使用方式</label>
                            <div className="grid h-10 grid-cols-2 border border-stone-200 bg-stone-50 p-1">
                              <button type="button" className={`text-sm ${String(provider.domain_mode || "round_robin") === "round_robin" ? "bg-white font-medium text-stone-900 shadow-sm" : "text-stone-500"}`} onClick={() => updateProvider(index, { domain_mode: "round_robin" })} disabled={isRuntimeBusy}>轮询</button>
                              <button type="button" className={`text-sm ${String(provider.domain_mode || "round_robin") === "sequential" ? "bg-white font-medium text-stone-900 shadow-sm" : "text-stone-500"}`} onClick={() => updateProvider(index, { domain_mode: "sequential" })} disabled={isRuntimeBusy}>顺序</button>
                            </div>
                          </div>
                        </>
                      ) : null}
                      {type === "ddg_mail" ? (
                        <>
                        <div className="space-y-2">
                          <label className="text-sm text-stone-700">DDG Token <span className="text-red-400">*</span></label>
                          <Input value={String(provider.ddg_token || "")} onChange={(event) => updateProvider(index, { ddg_token: event.target.value })} className="h-10 rounded-xl border-stone-200 bg-white" disabled={isRuntimeBusy} placeholder="DuckDuckGo Email Protection 的 Bearer Token" />
                        </div>
                        <div className="space-y-2">
                          <label className="text-sm text-stone-700">CF Inbox JWT <span className="text-red-400">*</span></label>
                          <Input value={String(provider.cf_inbox_jwt || "")} onChange={(event) => updateProvider(index, { cf_inbox_jwt: event.target.value })} className="h-10 rounded-xl border-stone-200 bg-white" disabled={isRuntimeBusy} placeholder="CF 临时邮箱后端的固定收件箱 JWT（DDG 转发目标）" />
                        </div>
                        <div className="rounded-lg border border-amber-200 bg-amber-50 p-3 text-xs text-amber-800">
                          <p className="font-medium mb-1">使用说明</p>
                          <ol className="list-decimal list-inside space-y-0.5">
                            <li>先在 <a href="https://duckduckgo.com/email/" target="_blank" className="underline">DuckDuckGo Email Protection</a> 登录并设置转发目标为 CF 收件箱地址</li>
                            <li>DDG Token 从浏览器 DevTools → Network → quack.duckduckgo.com 请求中获取 <code className="bg-amber-100 px-1 rounded">Authorization: Bearer</code></li>
                            <li>CF Inbox JWT 从 CF 临时邮箱后端创建固定收件箱后获取</li>
                            <li>所有 @duck.com 别名收到的邮件会转发到同一个 CF 收件箱，系统按 To: 头自动匹配</li>
                          </ol>
                        </div>
                        </>
                      ) : null}
                      {type === "inbucket" ? (
                        <label className="flex items-center gap-3 pt-8 text-sm text-stone-700">
                          <Checkbox checked={Boolean(provider.random_subdomain ?? true)} onCheckedChange={(checked) => updateProvider(index, { random_subdomain: Boolean(checked) })} disabled={isRuntimeBusy} />
                          启用随机子域名
                        </label>
                      ) : null}
                      {type === "tempmail_lol" || type === "moemail" || type === "duckmail" || type === "gptmail" || type === "yyds_mail" ? (
                        <div className="space-y-2">
                          <label className="text-sm text-stone-700">API Key</label>
                          <Input value={String(provider.api_key || "")} onChange={(event) => updateProvider(index, { api_key: event.target.value })} className="h-10 rounded-xl border-stone-200 bg-white" disabled={isRuntimeBusy} />
                        </div>
                      ) : null}
                      {type === "duckmail" || type === "gptmail" ? (
                        <div className="space-y-2">
                          <label className="text-sm text-stone-700">Default Domain</label>
                          <Input value={String(provider.default_domain || "")} onChange={(event) => updateProvider(index, { default_domain: event.target.value })} placeholder={type === "duckmail" ? "duckmail.sbs" : ""} className="h-10 rounded-xl border-stone-200 bg-white" disabled={isRuntimeBusy} />
                        </div>
                      ) : null}
                      {type === "yyds_mail" ? (
                        <>
                          <div className="space-y-2">
                            <label className="text-sm text-stone-700">Subdomain</label>
                            <Input value={String(provider.subdomain || "")} onChange={(event) => updateProvider(index, { subdomain: event.target.value })} className="h-10 rounded-xl border-stone-200 bg-white" disabled={isRuntimeBusy} />
                          </div>
                          <label className="flex items-center gap-3 pt-8 text-sm text-stone-700">
                            <Checkbox checked={Boolean(provider.wildcard)} onCheckedChange={(checked) => updateProvider(index, { wildcard: Boolean(checked) })} disabled={isRuntimeBusy} />
                            Wildcard
                          </label>
                        </>
                      ) : null}
                      {type === "outlook_token" ? (
                        <>
                          <div className="space-y-2">
                            <label className="text-sm text-stone-700">读取方式</label>
                            <Select value={String(provider.mode || "graph")} onValueChange={(value) => updateProvider(index, { mode: value })} disabled={isRuntimeBusy}>
                              <SelectTrigger className="h-10 rounded-xl border-stone-200 bg-white">
                                <SelectValue />
                              </SelectTrigger>
                              <SelectContent>
                                <SelectItem value="graph">Graph API</SelectItem>
                                <SelectItem value="imap">IMAP (XOAUTH2)</SelectItem>
                                <SelectItem value="auto">自动 (Graph→IMAP)</SelectItem>
                              </SelectContent>
                            </Select>
                          </div>
                          {String(provider.mode || "graph") !== "graph" ? (
                            <div className="space-y-2">
                              <label className="text-sm text-stone-700">IMAP Host</label>
                              <Input value={String(provider.imap_host || "outlook.office365.com")} onChange={(event) => updateProvider(index, { imap_host: event.target.value })} className="h-10 rounded-xl border-stone-200 bg-white" disabled={isRuntimeBusy} />
                            </div>
                          ) : null}
                          <label className="flex items-center gap-3 pt-8 text-sm text-stone-700">
                            <Checkbox checked={Boolean(provider.alias_enabled)} onCheckedChange={(checked) => updateProvider(index, { alias_enabled: Boolean(checked) })} disabled={isRuntimeBusy} />
                            启用 Outlook/Hotmail Plus Alias
                          </label>
                          {Boolean(provider.alias_enabled) ? (
                            <>
                              <div className="space-y-2">
                                <label className="text-sm text-stone-700">每个邮箱生成别名数</label>
                                <Input type="number" min={0} max={200} value={Number(provider.alias_per_email ?? 5)} onChange={(event) => updateProvider(index, { alias_per_email: Math.max(0, Math.min(200, Number(event.target.value) || 0)) })} className="h-10 rounded-xl border-stone-200 bg-white" disabled={isRuntimeBusy} />
                              </div>
                              <div className="space-y-2">
                                <label className="text-sm text-stone-700">别名前缀</label>
                                <Input value={String(provider.alias_prefix || "c2api")} onChange={(event) => updateProvider(index, { alias_prefix: event.target.value })} placeholder="c2api" className="h-10 rounded-xl border-stone-200 bg-white" disabled={isRuntimeBusy} />
                              </div>
                              <label className="flex items-center gap-3 pt-8 text-sm text-stone-700">
                                <Checkbox checked={Boolean(provider.alias_include_original ?? true)} onCheckedChange={(checked) => updateProvider(index, { alias_include_original: Boolean(checked) })} disabled={isRuntimeBusy} />
                                同时使用原始邮箱
                              </label>
                            </>
                          ) : null}
                        </>
                      ) : null}
                    </div>

                    {type !== "outlook_token" && (latchedDisabled || Number(health.consecutive_failures || 0) > 0 || healthDomains.length > 0) ? (
                      <div className="space-y-2 border-t border-stone-100 pt-3 text-xs">
                        {healthDomains.length ? healthDomains.map((item) => (
                          <div key={String(item.domain)} className="flex flex-wrap items-center justify-between gap-2">
                            <span className={Boolean(item.latched_disabled) ? "text-rose-600" : "text-stone-600"}>
                              {String(item.domain)} · {Boolean(item.latched_disabled) ? "已禁用" : `连续失败 ${Number(item.consecutive_failures || 0)}`}
                            </span>
                            {(Boolean(item.latched_disabled) || Number(item.consecutive_failures || 0) > 0) ? (
                              <Button type="button" variant="outline" className="h-7 rounded-md border-stone-200 bg-white px-2 text-xs" onClick={() => void resetMailHealth(providerId, String(item.domain))} disabled={isSaving}>
                                <RotateCcw className="size-3" />恢复域名
                              </Button>
                            ) : null}
                          </div>
                        )) : null}
                        {(latchedDisabled || Number(health.consecutive_failures || 0) > 0) ? (
                          <div className="flex flex-wrap items-center justify-between gap-2">
                            <span className="text-stone-500">{String(health.last_error || "自动健康状态已记录")}</span>
                            <Button type="button" variant="outline" className="h-7 rounded-md border-stone-200 bg-white px-2 text-xs" onClick={() => void resetMailHealth(providerId)} disabled={isSaving}>
                              <RotateCcw className="size-3" />恢复渠道
                            </Button>
                          </div>
                        ) : null}
                      </div>
                    ) : null}

                    {type === "outlook_token" ? (() => {
                      const stats = (provider.mailboxes_stats || {}) as Record<string, number>;
                      const savedCount = Number(provider.mailboxes_count || 0);
                      const savedBaseCount = Number(provider.mailboxes_base_count || savedCount);
                      const savedAliasCount = Number(provider.mailboxes_alias_count || 0);
                      const preview = Array.isArray(provider.mailboxes_preview) ? (provider.mailboxes_preview as string[]) : [];
                      const aliasPreview = Array.isArray(provider.alias_preview) ? (provider.alias_preview as string[]) : [];
                      const pendingLines = String(provider.mailboxes || "").split(/\r?\n/).filter((line) => line.includes("----") && line.split("----").length >= 4);
                      const pendingCount = pendingLines.length;
                      const aliasEnabled = Boolean(provider.alias_enabled);
                      const aliasesPerEmail = Math.max(0, Math.min(200, Number(provider.alias_per_email ?? 5) || 0));
                      const includeOriginal = Boolean(provider.alias_include_original ?? true);
                      const supportedPending = pendingLines.filter((line) => {
                        const email = line.split("----", 1)[0]?.trim().toLowerCase() || "";
                        const domain = email.split("@")[1] || "";
                        return domain === "outlook.com" || domain === "hotmail.com" || domain === "live.com" || domain === "msn.com" || domain.startsWith("hotmail.") || domain.startsWith("outlook.");
                      }).length;
                      const pendingExpanded = aliasEnabled && aliasesPerEmail > 0 ? supportedPending * aliasesPerEmail + (includeOriginal ? pendingCount : 0) : pendingCount;
                      return (
                        <div className="space-y-2">
                          <label className="flex items-center justify-between text-sm text-stone-700">
                            <span>邮箱池导入 <span className="text-red-400">*</span></span>
                            <span className="text-xs text-stone-400">基础邮箱 {savedBaseCount} 个 · 可用地址 {savedCount} 个{savedAliasCount ? `（别名 ${savedAliasCount}）` : ""}{pendingCount ? ` · 待导入 ${pendingCount} 个，预计展开 ${pendingExpanded} 个` : ""}</span>
                          </label>
                          <Textarea value={String(provider.mailboxes || "")} onChange={(event) => updateProvider(index, { mailboxes: event.target.value })} placeholder={"每行一个邮箱，格式：\n邮箱----密码----client_id----refresh_token\n（出于安全，已保存的密码/refresh_token 不会回显；此处仅用于新增或覆盖）"} className="min-h-32 rounded-xl border-stone-200 bg-white font-mono text-xs" disabled={isRuntimeBusy} />
                          <div className="flex flex-wrap items-center gap-1.5 text-xs">
                            <span className="rounded-md bg-stone-100 px-2 py-1 text-stone-600">未使用 {stats.unused ?? 0}</span>
                            <span className="rounded-md bg-blue-50 px-2 py-1 text-blue-600">占用中 {stats.in_use ?? 0}</span>
                            <span className="rounded-md bg-emerald-50 px-2 py-1 text-emerald-700">已用 {stats.used ?? 0}</span>
                            <span className="rounded-md bg-amber-50 px-2 py-1 text-amber-700">token失效 {stats.token_invalid ?? 0}</span>
                            <span className="rounded-md bg-rose-50 px-2 py-1 text-rose-600">失败 {stats.failed ?? 0}</span>
                          </div>
                          {preview.length ? (
                            <p className="text-xs text-stone-400">已保存邮箱（脱敏）：{preview.slice(0, 8).join("、")}{preview.length > 8 ? ` 等 ${preview.length} 个` : ""}</p>
                          ) : null}
                          {aliasPreview.length ? (
                            <p className="text-xs text-stone-400">别名示例（脱敏）：{aliasPreview.join("、")}</p>
                          ) : null}
                          <div className="flex flex-wrap items-center gap-2">
                            <Button type="button" variant="outline" className="h-8 rounded-lg border-stone-200 bg-white px-3 text-xs text-stone-700" onClick={() => void resetOutlookPool("failed")} disabled={isRuntimeBusy}>
                              清除失败/占用状态
                            </Button>
                            <Button type="button" variant="outline" className="h-8 rounded-lg border-amber-200 bg-white px-3 text-xs text-amber-700 hover:bg-amber-50" onClick={() => { if (window.confirm("确定要从 Outlook 邮箱池中删除所有未使用邮箱吗？此操作会移除这些已保存凭据。")) void resetOutlookPool("unused"); }} disabled={isRuntimeBusy}>
                              清空未使用
                            </Button>
                            <Button type="button" variant="outline" className="h-8 rounded-lg border-rose-200 bg-white px-3 text-xs text-rose-600 hover:bg-rose-50" onClick={() => { if (window.confirm("确定要重置整个 Outlook 邮箱池状态吗？所有邮箱会被标记为可重新使用。")) void resetOutlookPool("all"); }} disabled={isRuntimeBusy}>
                              重置全部状态
                            </Button>
                          </div>
                          <p className="text-xs text-stone-500">每个原始邮箱或别名仅成功注册一次（状态记录在 data/outlook_token_used.json）。别名共用原邮箱的 refresh token，验证码会按实际收件地址隔离。</p>
                        </div>
                      );
                    })() : null}

                    {type === "cloudmail_gen" || type === "tempmail_lol" || type === "cloudflare_temp_email" || type === "moemail" || type === "inbucket" || type === "yyds_mail" || type === "ddg_mail" ? (
                      <div className="space-y-2">
                        <label className="text-sm text-stone-700">{type === "cloudmail_gen" ? "邮箱域名" : type === "inbucket" ? "基础域名列表" : "Domain"}</label>
                        <Textarea value={domains} onChange={(event) => updateProvider(index, { domain: event.target.value.split(/[\n,]/).map((item) => item.trim()) })} placeholder={type === "cloudmail_gen" ? "每行一个域名，留空则使用服务默认域名" : type === "inbucket" ? "每行一个基础域名，系统会自动生成随机子域名" : type === "moemail" ? "每行一个域名" : "每行一个域名，留空则使用服务默认域名"} className="min-h-20 rounded-xl border-stone-200 bg-white font-mono text-xs" disabled={isRuntimeBusy} />
                      </div>
                    ) : null}
                    {type === "cloudmail_gen" ? (
                      <div className="space-y-2">
                        <label className="text-sm text-stone-700">子域名（支持多个）</label>
                        <Textarea value={subdomains} onChange={(event) => updateProvider(index, { subdomain: event.target.value.split(/[\n,]/).map((item) => item.trim()) })} placeholder="每行一个子域名前缀，留空则直接使用主域名" className="min-h-20 rounded-xl border-stone-200 bg-white font-mono text-xs" disabled={isRuntimeBusy} />
                      </div>
                    ) : null}
                  </div>
                );
              })}
            </div>
          </div>

      </section>

      <section className="flex min-h-0 min-w-0 flex-col p-4">
        <div className="space-y-3">
            <div className="flex items-start justify-between gap-3">
              <div>
                <h2 className="text-lg font-semibold tracking-tight">运行结果</h2>
                <p className="mt-1 text-sm text-stone-500">SSE 实时推送当前状态。</p>
              </div>
              <Badge variant={isStopping ? "warning" : isEnabled ? "success" : "secondary"} className="rounded-md">
                {isStopping ? "停止中" : isEnabled ? "运行中" : "已停止"}
              </Badge>
            </div>
            <div className="grid grid-cols-4 gap-2">
              {[
                ["成功 / 成功率", `${stats.success} / ${stats.success_rate || 0}%`],
                ["失败", stats.fail],
                ["完成", stats.done],
                ["运行 / 线程", `${stats.running} / ${stats.threads}`],
                ["运行时间", `${stats.elapsed_seconds || 0}s`],
                ["平均注册单个", `${stats.avg_seconds || 0}s`],
                ["当前额度", stats.current_quota || 0],
                ["正常账号", stats.current_available || 0],
              ].map(([label, value]) => (
                <div key={label} className="border border-stone-200 bg-white/70 px-3 py-2">
                  <div className="text-xs text-stone-400">{label}</div>
                  <div className="mt-1 text-base font-semibold text-stone-800">{value}</div>
                </div>
              ))}
            </div>
            <div className="grid grid-cols-3 gap-2">
              <Button className="h-10 rounded-xl bg-stone-950 px-3 text-white hover:bg-stone-800" onClick={() => void toggle()} disabled={isSaving || isStopping || browserUnavailable}>
                {isSaving ? <LoaderCircle className="size-4 animate-spin" /> : isEnabled ? <Square className="size-4" /> : <Play className="size-4" />}
                {isStopping ? "停止中" : isEnabled ? "停止" : "启动"}
              </Button>
              <Button variant="outline" className="h-10 rounded-xl border-stone-200 bg-white px-3 text-stone-700" onClick={() => void reset()} disabled={isSaving || isRuntimeBusy}>
                <RotateCcw className="size-4" />
                重置
              </Button>
              <Button variant="outline" className="h-10 rounded-xl border-stone-200 bg-white px-3 text-stone-700" onClick={() => void save()} disabled={isSaving || isRuntimeBusy}>
                <Save className="size-4" />
                保存
              </Button>
            </div>
            <div className="flex items-center gap-2 border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">
              <AlertTriangle className="size-4 shrink-0" />
              启动之前注意先保存配置。
            </div>
        </div>

        <div className="mt-4 flex min-h-0 flex-1 flex-col space-y-3 overflow-hidden border-t border-stone-200 pt-4">
            <div className="flex items-center justify-between">
              <div>
                <h3 className="text-sm font-semibold text-stone-900">实时日志</h3>
                <p className="mt-1 text-xs text-amber-700">遇到 HTTP 状态码 400 等错误，基本是邮箱滥用被封，需要更换新的域名邮箱。</p>
              </div>
              <Badge variant="secondary" className="rounded-md">
                {logs.length}
              </Badge>
            </div>
            <div className="min-h-0 flex-1 overflow-y-auto border border-stone-200 bg-white/70 p-3 font-mono text-xs leading-6">
              {logs.length === 0 ? (
                <div className="text-stone-500">暂无日志</div>
              ) : (
                logs.slice().reverse().map((item, index) => (
                  <div key={`${item.time}-${index}`} className={item.level === "red" ? "text-rose-600" : item.level === "green" ? "text-emerald-700" : item.level === "yellow" ? "text-amber-700" : "text-stone-700"}>
                    <span className="text-stone-400">{new Date(item.time).toLocaleTimeString()}</span>
                    <span className="pl-2">{item.text}</span>
                  </div>
                ))
              )}
            </div>
        </div>
      </section>
    </div>
  );
}
