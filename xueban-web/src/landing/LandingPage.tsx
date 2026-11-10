import { FormEvent, useEffect, useRef, useState } from "react";
// 落地页只负责介绍与跳转；登录/工作台由我们自己的 /app 处理。
const useAuthState = () => ({ ready: true, user: null as { display_name?: string; email?: string } | null });
const displayNameOf = (_user: unknown) => "";
const authSubscribe = async (_payload: unknown) => undefined;
const samplePhoto1 = "/landing/desk-study.jpg";
const samplePhoto2 = "/landing/study-room.jpg";
import {
  ArrowRight,
  BarChart3,
  BrainCircuit,
  BriefcaseBusiness,
  Check,
  ChevronDown,
  Database,
  FileCheck2,
  FileText,
  Image as ImageIcon,
  Layers3,
  Link2,
  Megaphone,
  Menu,
  MessageSquareText,
  MousePointer2,
  Network,
  Pause,
  Play,
  RefreshCw,
  ShieldCheck,
  ShoppingBag,
  Sparkles,
  Target,
  Truck,
  UsersRound,
  Wallet,
  X,
  Zap,
} from "lucide-react";

const steps = [
  {
    id: "01",
    title: "录入学情需求",
    label: "BRIEF",
    description: "选择班级或学生，说明学科、范围与关注点，上传成绩单、作业与课程材料。",
    result: "结构化学情需求",
    icon: MessageSquareText,
  },
  {
    id: "02",
    title: "教学主管拆解",
    label: "PLAN",
    description: "识别任务范围，生成执行计划并分配给课程内容、学情分析、学习规划等教研员。",
    result: "任务计划与执行 DAG",
    icon: BrainCircuit,
  },
  {
    id: "03",
    title: "学科教研员协同",
    label: "ANALYZE",
    description: "课程内容教研员梳理学习要求，学情分析师检查数据质量并分析个体与群体表现，学习规划师据此给出建议。",
    result: "内容清单 · 学情摘要 · 学习建议",
    icon: Network,
  },
  {
    id: "04",
    title: "核对依据并交付",
    label: "DELIVER",
    description: "在关键节点人工确认，核对统计口径与材料出处，导出学情报告与学习方案。",
    result: "可核对、可执行、可交付",
    icon: FileCheck2,
  },
];

const agents = [
  {
    role: "课程内容教研员",
    en: "CURRICULUM SPECIALIST",
    task: "课程标准 · 内容梳理 · 材料出处",
    color: "#2777e8",
    icon: FileText,
  },
  {
    role: "学情数据分析师",
    en: "LEARNING ANALYST",
    task: "数据质量 · 个体与群体表现",
    color: "#8e6cff",
    icon: BarChart3,
  },
  {
    role: "学习规划师",
    en: "LEARNING DESIGNER",
    task: "学习路径 · 练习匹配 · 先后安排",
    color: "#2ec4a6",
    icon: Target,
  },
  {
    role: "练习与测评设计",
    en: "ASSESSMENT DESIGNER",
    task: "题目匹配 · 难度分层 · 反馈设计",
    color: "#f5a623",
    icon: Sparkles,
  },
  {
    role: "质量审核",
    en: "QUALITY REVIEW",
    task: "依据核对 · 建议合理性",
    color: "#12b76a",
    icon: FileCheck2,
  },
  {
    role: "数据与合规审核",
    en: "DATA COMPLIANCE",
    task: "隐私保护 · 来源标注 · 内容合规",
    color: "#667085",
    icon: ShieldCheck,
  },
];

const plans = [
  {
    name: "体验版",
    price: "免费",
    features: ["3 位预设 AI 教研员", "500 基础积分"],
  },
  {
    name: "教研组版",
    price: "66RMB/ 月",
    features: ["1000+2000 赠送积分", "多班级学情管理", "校本课程资源库"],
    featured: true,
  },
  {
    name: "学校定制版",
    price: "定制",
    features: ["专属学科教研员", "资源库迁移", "定制工作流与私有部署"],
  },
];

const faqs = [
  {
    q: "智慧学伴和通用大模型有什么不同？",
    a: "通用大模型擅长单次问答；智慧学伴把模型能力组织成有分工、有依据、有复核的教研流程，把课程标准、学情数据与学习建议串成可核对的成果。",
  },
  {
    q: "它真的能分析我班上的学情吗？",
    a: "可以。导入成绩单、作业与课堂记录后，学情分析师会先做数据质量检查，再给出个体与群体的表现摘要，并明确说明哪些结论因为记录不足而不能下。",
  },
  {
    q: "AI 会直接把建议发给学生或家长吗？",
    a: "不会。学习方案与学情报告默认需要老师确认；涉及导出真实学生数据、对外发布或删除记录时，系统会停下来等你审批。",
  },
  {
    q: "能否沉淀我们学校自己的教学方法？",
    a: "可以。学校定制版支持把校本课程材料、评价标准与教研经验沉淀成课程资源库、专属 Skill 与工作流，换班级、换学期都能复用。",
  },
];

function LandingApp() {
  const [menuOpen, setMenuOpen] = useState(false);
  const [activeStep, setActiveStep] = useState(1);
  const [modal, setModal] = useState<"demo" | "plan" | null>(null);
  const [selectedPlan, setSelectedPlan] = useState("教研组版");
  const [submitted, setSubmitted] = useState(false);
  const [openFaq, setOpenFaq] = useState(0);
  const [authMode, setAuthMode] = useState<"login" | "register">("login");
  const [prefillEmail, setPrefillEmail] = useState("");
  const [newsDone, setNewsDone] = useState(false);
  const auth = useAuthState();

  useEffect(() => {
    document.body.style.overflow = modal ? "hidden" : "";
    return () => {
      document.body.style.overflow = "";
    };
  }, [modal]);

  function scrollTo(id: string) {
    document.getElementById(id)?.scrollIntoView({ behavior: "smooth" });
    setMenuOpen(false);
  }

  function openPlan(plan: string) {
    setSelectedPlan(plan);
    setSubmitted(false);
    setModal("plan");
  }

  function openAuth(mode: "login" | "register", email = "") {
    setAuthMode(mode);
    setPrefillEmail(email);
    // 所有入口统一进入我们的工作台（未登录时由工作台展示登录页）。
    window.location.assign("/app");
  }

  // 入口合并：登录与进入产品合一。未登录时先弹登录，登录成功后直接进工作台。
  function enterWorkbench() {
    window.location.assign("/app");
  }

  function submitForm(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    const email = String(data.get("email") ?? "").trim();
    const name = String(data.get("name") ?? "").trim();
    const stage = String(data.get("stage") ?? "").trim();
    const need = String(data.get("need") ?? "").trim();
    // 留资（失败静默——介绍页可能没有后端），随后进入成功态
    void authSubscribe({
      email,
      name: name || undefined,
      source: modal === "demo" ? "landing-demo" : `landing-plan:${selectedPlan}`,
      note: [stage && `阶段:${stage}`, need && `需求:${need}`].filter(Boolean).join(" | ") || undefined,
    });
    setPrefillEmail(email);
    setSubmitted(true);
    form.reset();
  }

  function submitNews(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    const email = String(data.get("email") ?? "").trim();
    if (!email) return;
    void authSubscribe({ email, source: "landing-newsletter" });
    setPrefillEmail(email);
    setNewsDone(true);
    form.reset();
  }

  return (
    <main className="landing-root">
      <nav className="top-nav">
        <div className="nav-inner">
          <button className="brand" onClick={() => scrollTo("home")} aria-label="智慧学伴首页">
            <Logo />
            <span>智慧学伴</span>
          </button>
          <div className="nav-links">
            <button onClick={() => scrollTo("solutions")}>学习场景</button>
            <button onClick={() => scrollTo("workflow")}>诊断链路</button>
            <button onClick={() => scrollTo("agents")}>教研团队</button>
            <button onClick={() => scrollTo("outcomes")}>学情沉淀</button>
            <button onClick={() => scrollTo("cases")}>使用场景</button>
            <button onClick={() => scrollTo("faq")}>常见问题</button>
          </div>
          <div className="nav-actions">
            <button className="nav-login" onClick={() => setModal("demo")}>
              联系我们
            </button>
            <button className="nav-cta" onClick={enterWorkbench}>
              {auth.user ? `${displayNameOf(auth.user)} · 工作台` : "进入工作台"}
            </button>
          </div>
          <button
            className="mobile-menu"
            onClick={() => setMenuOpen(!menuOpen)}
            aria-label="打开菜单"
          >
            {menuOpen ? <X /> : <Menu />}
          </button>
        </div>
        {menuOpen && (
          <div className="mobile-panel">
            {[
              ["学习场景", "solutions"],
              ["诊断链路", "workflow"],
              ["教研团队", "agents"],
              ["学情沉淀", "outcomes"],
              ["使用场景", "cases"],
              ["常见问题", "faq"],
            ].map(([label, id]) => (
              <button key={id} onClick={() => scrollTo(id)}>
                {label}
              </button>
            ))}
            <button className="nav-login" onClick={() => setModal("demo")}>
              联系我们
            </button>
            <button className="nav-cta" onClick={enterWorkbench}>
              {auth.user ? `${displayNameOf(auth.user)} · 工作台` : "进入工作台"}
            </button>
          </div>
        )}
      </nav>

      <header id="home" className="hero">
        <div className="hero-orbit orbit-one" />
        <div className="hero-orbit orbit-two" />
        <div className="page-shell hero-grid">
          <div className="hero-copy">
            <h1>
              一位老师，
              <br />
              也能带一支
              <br />
              <em>AI 教研团队。</em>
            </h1>
            <div className="hero-actions">
              <button className="button-primary" onClick={enterWorkbench}>
                进入工作台
              </button>
              <button className="button-outline" onClick={() => setModal("demo")}>
                联系我们
              </button>
              <button className="button-rect" onClick={() => scrollTo("workflow")}>
                看看如何工作 <ArrowRight size={15} />
              </button>
            </div>
          </div>
          <ProductVisual />
        </div>
      </header>

      <section id="solutions" className="section problem-section">
        <div className="page-shell">
          <div className="section-intro">
            <div>
              <h2>
                真实教学场景，
                <br />
                是否遇到以下问题？
              </h2>
            </div>
          </div>
          <div className="problem-grid">
            {[
              ["01", "学情数据散落各处", "成绩单、作业、课堂记录分散在不同表格里，很难拼出完整的学习画像。", Network],
              ["02", "课程要求难以落到具体学生", "课程标准和材料很多，落到某个学生该练什么，仍然靠经验判断。", Layers3],
              ["03", "分析结论难以执行", "知道学生薄弱，却给不出具体的学习内容、练习与先后安排。", ShieldCheck],
              ["04", "教学经验无法沉淀", "有效做法留在个人笔记里，换班级、换学期又要从头摸索。", Database],
            ].map(([num, title, copy, Icon]) => {
              const ItemIcon = Icon as typeof Network;
              return (
                <article key={num as string}>
                  <span>{num as string}</span>
                  <ItemIcon />
                  <div>
                    <h3>{title as string}</h3>
                    <p>{copy as string}</p>
                  </div>
                </article>
              );
            })}
          </div>
        </div>
      </section>

      <section id="workflow" className="section workflow-section">
        <div className="page-shell">
          <div className="section-intro light">
            <div>
              <h2>
                三段任务的链路，
                <br />
                帮你把学情变成行动。
              </h2>
            </div>
          </div>
          <div className="workflow-layout">
            <div className="step-list">
              {steps.map((step, index) => (
                <button
                  key={step.id}
                  onClick={() => setActiveStep(index)}
                  className={activeStep === index ? "active" : ""}
                >
                  <span>{step.id}</span>
                  <div>
                    <small>{step.label}</small>
                    <strong>{step.title}</strong>
                  </div>
                </button>
              ))}
            </div>
            <div className="step-detail">
              <div className="step-topline">
                <span>WORKFLOW / {steps[activeStep].id}</span>
                <span className="live-dot">LIVE</span>
              </div>
              <div className="step-icon">
                {(() => {
                  const Icon = steps[activeStep].icon;
                  return <Icon size={40} />;
                })()}
              </div>
              <p className="step-label">{steps[activeStep].label}</p>
              <h3>{steps[activeStep].title}</h3>
              <p className="step-description">{steps[activeStep].description}</p>
              <div className="step-result">
                <Check size={17} />
                <div>
                  <small>本阶段产出</small>
                  <strong>{steps[activeStep].result}</strong>
                </div>
              </div>
              <div className="flow-line">
                {steps.map((_, index) => (
                  <span key={index} className={index <= activeStep ? "done" : ""} />
                ))}
              </div>
            </div>
          </div>
        </div>
      </section>

      <section id="agents" className="section agents-section">
        <div className="page-shell">
          <div className="section-intro">
            <div>
              <h2>
                AI 教研团队，
                <br />
                覆盖十个学科。
              </h2>
            </div>
          </div>
          <div className="agent-grid">
            {agents.map((agent, index) => {
              const Icon = agent.icon;
              return (
                <article
                  className="agent-card"
                  key={agent.role}
                  style={{ "--agent-color": agent.color } as React.CSSProperties}
                >
                  <div className="agent-number">A{index + 1}</div>
                  <div className="agent-avatar">
                    <Icon size={31} />
                  </div>
                  <p>{agent.en}</p>
                  <h3>{agent.role}</h3>
                  <div className="agent-task">{agent.task}</div>
                  <div className="agent-status">
                    <span /> READY TO WORK
                  </div>
                </article>
              );
            })}
          </div>
          <div className="human-loop">
            <div className="human-badge">
              <MousePointer2 />
            </div>
            <div>
              <p>HUMAN IN THE LOOP</p>
              <h3>AI 负责分析与建议，你负责判断——学习方案与最终报告都由你确认。</h3>
            </div>
          </div>
        </div>
      </section>

      <section id="outcomes" className="section outcomes-section">
        <div className="page-shell outcomes-grid">
          <div className="outcome-copy">
            <h2>
              用得越久，
              <br />
              越懂你的学生。
            </h2>
            <div className="outcome-metrics">
              <div>
                <strong>1</strong>
                <span>份学生学情档案</span>
              </div>
              <div>
                <strong>∞</strong>
                <span>次复用教学方法</span>
              </div>
            </div>
          </div>
          <div className="knowledge-visual">
            <div className="knowledge-core">
              <Database />
              <span>课程资源库</span>
              <small>持续学习中</small>
            </div>
            <div className="knowledge-node n1">
              <MessageSquareText />
              <span>课堂反馈</span>
            </div>
            <div className="knowledge-node n2">
              <FileCheck2 />
              <span>学情报告</span>
            </div>
            <div className="knowledge-node n3">
              <Layers3 />
              <span>教学策略</span>
            </div>
            <div className="knowledge-node n4">
              <Target />
              <span>学生画像</span>
            </div>
            <svg viewBox="0 0 600 480" aria-hidden="true">
              <path d="M300 240 L130 110 M300 240 L480 100 M300 240 L115 375 M300 240 L485 375" />
            </svg>
          </div>
        </div>
      </section>

      <section className="testimonial-section slogan-band">
        <div className="page-shell testimonial">
          <p>YOUR CLASS. YOUR METHOD. YOUR AI TEAM.</p>
          <blockquote>让一位老师，也能拥有一支专业的 AI 教研团队。</blockquote>
        </div>
      </section>

      <section id="cases" className="section sample-section">
        <div className="page-shell">
          <div className="section-intro">
            <div>
              <h2>
                使用场景：晚自习前，
                <br />
                完成一次学情诊断。
              </h2>
            </div>
          </div>
          <div className="sample-photos">
            <figure>
              <img src={samplePhoto1} alt="学生在书桌前对照学情报告订正作业" />
              <figcaption>书桌前 · 逐题订正</figcaption>
            </figure>
            <figure>
              <img src={samplePhoto2} alt="学生在自习室专注复盘当日学习内容" />
              <figcaption>自习室 · 专注复盘</figcaption>
            </figure>
          </div>
          <SampleDemo />
        </div>
      </section>

      <section id="faq" className="section faq-section">
        <div className="page-shell faq-layout">
          <div>
            <h2>常见问题</h2>
            <p>
              <button onClick={() => setModal("demo")}>联系我们</button>
            </p>
          </div>
          <div className="faq-list">
            {faqs.map((faq, index) => (
              <article key={faq.q} className={openFaq === index ? "open" : ""}>
                <button onClick={() => setOpenFaq(openFaq === index ? -1 : index)}>
                  <span>{String(index + 1).padStart(2, "0")}</span>
                  <strong>{faq.q}</strong>
                  <ChevronDown />
                </button>
                <div>
                  <p>{faq.a}</p>
                </div>
              </article>
            ))}
          </div>
        </div>
      </section>

      <section id="pricing" className="section pricing-section">
        <div className="page-shell">
          <div className="pricing-heading">
            <h2>从一次学情诊断开始，搭起你的 AI 教研组。</h2>
            <p>以下为产品规划参考，正式价格与方案以公布为准。</p>
          </div>
          <div className="pricing-grid">
            {plans.map((plan) => (
              <article key={plan.name} className={`price-card ${plan.featured ? "featured" : ""}`}>
                <h3>{plan.name}</h3>
                <div className="plan-price">{plan.price}</div>
                <ul>
                  {plan.features.map((feature) => (
                    <li key={feature}>{feature}</li>
                  ))}
                </ul>
                <button onClick={() => openPlan(plan.name)}>
                  {plan.name === "体验版"
                    ? "免费开始"
                    : plan.name === "教研组版"
                      ? "开始 14 天试用"
                      : "联系方案顾问"}
                </button>
              </article>
            ))}
          </div>
        </div>
      </section>

      <footer>
        <div className="page-shell footer-main">
          <div className="footer-brand">
            <div className="brand">
              <Logo />
              <span>智慧学伴</span>
            </div>
            <p>让一位老师，也能拥有一支专业的 AI 教研团队。</p>
          </div>
          <div>
            <strong>产品</strong>
            <button onClick={() => scrollTo("workflow")}>如何工作</button>
            <button onClick={() => scrollTo("agents")}>AI 教研员</button>
            <button onClick={() => scrollTo("pricing")}>价格方案</button>
          </div>
          <div>
            <strong>资源</strong>
            <a href="#faq">帮助中心</a>
            <a href="#about">关于我们</a>
            <a href="mailto:hello@xueban.ai">联系我们</a>
          </div>
          <div className="footer-news">
            <strong>获取产品进展</strong>
            <p>{newsDone ? "已收到，感谢订阅！" : "订阅内测与产品更新"}</p>
            {newsDone ? (
              <button className="footer-news-done" onClick={() => openAuth("register", prefillEmail)}>
                注册账号，直接开始
              </button>
            ) : (
              <form onSubmit={submitNews}>
                <input name="email" type="email" required aria-label="邮箱地址" placeholder="你的邮箱" />
                <button aria-label="订阅">订阅</button>
              </form>
            )}
          </div>
        </div>
        <div className="page-shell footer-bottom">
          <span>© 2026 智慧学伴. All rights reserved.</span>
          <div>
            <a href="#privacy">隐私政策</a>
            <a href="#terms">服务条款</a>
          </div>
        </div>
      </footer>

      {modal && (
        <div
          className="modal-backdrop"
          role="dialog"
          aria-modal="true"
          aria-labelledby="modal-title"
          onMouseDown={() => setModal(null)}
        >
          <div className="contact-modal" onMouseDown={(event) => event.stopPropagation()}>
            <button className="modal-close" onClick={() => setModal(null)} aria-label="关闭">
              <X />
            </button>
            {submitted ? (
              <div className="success-state">
                <span>
                  <Check />
                </span>
                <p>SUBMITTED</p>
                <h2>已收到你的申请</h2>
                <p>感谢关注智慧学伴。我们会尽快通过邮件与你联系。</p>
                <button
                  className="button-primary"
                  onClick={() => {
                    setModal(null);
                    openAuth("register", prefillEmail);
                  }}
                >
                  注册账号，直接开始
                </button>
                <button className="sf-auth-demo" onClick={() => setModal(null)}>
                  暂不注册，先逛逛
                </button>
              </div>
            ) : (
              <>
                <h2 id="modal-title">
                  {modal === "demo"
                    ? "看看智慧学伴如何帮你完成这次学情诊断"
                    : `申请 ${selectedPlan}`}
                </h2>
                <p>留下你的信息，我们将在 1 个工作日内与你联系。</p>
                <form onSubmit={submitForm}>
                  <label>
                    你的称呼
                    <input name="name" required placeholder="怎么称呼你" />
                  </label>
                  <label>
                    工作邮箱
                    <input name="email" required type="email" placeholder="name@company.com" />
                  </label>
                  <label>
                    你目前的业务阶段
                    <select name="stage" defaultValue="">
                      <option value="" disabled>
                        请选择
                      </option>
                      <option>刚开始带班或备课</option>
                      <option>已有一两个班的教学数据</option>
                      <option>已有成熟的教学方法</option>
                    </select>
                  </label>
                  <label>
                    最想解决的问题
                    <textarea
                      name="need"
                      rows={3}
                      placeholder="例如：学情数据整理、个性化学习建议、教研沉淀…"
                    />
                  </label>
                  <button className="button-primary" type="submit">
                    提交申请
                  </button>
                </form>
              </>
            )}
          </div>
        </div>
      )}

      {/* 登录由 /app 处理 */}
    </main>
  );
}

function Logo() {
  return (
    <span className="logo-mark">
      <i />
      <i />
      <i />
    </span>
  );
}

function ProductVisual() {
  return (
    <div className="product-visual" style={{ cursor: "pointer" }} title="点击进入产品" onClick={() => { window.location.assign("/app"); }}>
      <div className="visual-window">
        <div className="window-bar">
          <div>
            <span />
            <span />
            <span />
          </div>
          <p>学情诊断中心</p>
          <small>xueban.ai</small>
        </div>
        <div className="window-body">
          <aside>
            <Logo />
            {[BriefcaseBusiness, UsersRound, Layers3, Database].map((Icon, index) => (
              <span key={index} className={index === 0 ? "active" : ""}>
                <Icon size={17} />
              </span>
            ))}
          </aside>
          <div className="project-content">
            <div className="project-head">
              <div>
                <small>ACTIVE DIAGNOSIS</small>
                <h3>七年级数学 · 有理数与整式</h3>
              </div>
              <span>分析中 · 68%</span>
            </div>
            <div className="progress">
              <i />
            </div>
            <div className="project-stats">
              <div>
                <span>12</span>
                <small>教研任务</small>
              </div>
              <div>
                <span>08</span>
                <small>已完成</small>
              </div>
              <div>
                <span>03</span>
                <small>教研员执行中</small>
              </div>
            </div>
            <div className="project-flow">
              <p>
                工作流 <span>实时同步</span>
              </p>
              {[
                ["学习内容梳理", "已完成", "#00b9a7"],
                ["学情分析", "审核中", "#ff5e3a"],
                ["学习建议", "执行中", "#8e6cff"],
              ].map(([name, state, color], index) => (
                <div key={name}>
                  <i style={{ background: color }}>{index + 1}</i>
                  <strong>{name}</strong>
                  <span>{state}</span>
                  {index < 2 && <b />}
                </div>
              ))}
            </div>
          </div>
        </div>
      </div>
      <div className="floating-card floating-agent">
        <span>
          <BrainCircuit />
        </span>
        <div>
          <small>教学主管</small>
          <strong>已拆解 12 个教研任务</strong>
        </div>
        <Check />
      </div>
      <div className="floating-card floating-deliver">
        <FileCheck2 />
        <div>
          <small>学情报告已更新</small>
          <strong>学情报告 V2.1</strong>
        </div>
        <span>刚刚</span>
      </div>
      <div className="visual-badge">
        <Zap size={15} fill="currentColor" /> 3 位教研员正在协作
      </div>
    </div>
  );
}


/* ================= 样品页：手作助残电商 Demo ================= */

type DemoEvent =
  | { kind: "ceo"; text: string }
  | { kind: "user"; text: string; chips: { icon: "image" | "file"; label: string }[] }
  | { kind: "note"; text: string }
  | { kind: "team"; members: string[][] }
  | {
      kind: "card";
      tone: "post" | "link" | "order" | "courier" | "profit";
      title: string;
      detail: string;
    }
  | { kind: "done"; text: string };

const demoEvents: DemoEvent[] = [
  {
    kind: "ceo",
    text: "你好，我是智慧学伴的教学主管 👋 告诉我班级、学科和你想了解的问题，再把成绩单或作业发给我。",
  },
  {
    kind: "user",
    text: "七年级 3 班，数学「有理数与整式」。想看看这次单元测哪些知识点需要巩固，并给薄弱学生一点学习建议。",
    chips: [
      { icon: "image", label: "单元测成绩单.png" },
      { icon: "file", label: "第三章作业.zip" },
    ],
  },
  {
    kind: "note",
    text: "已自动同步 2 份资料到课程资源库（成绩单与作业解析入库，团队随时调用）",
  },
  { kind: "ceo", text: "资料已入库，数据质量检查通过（缺考 2 人、重复记录 1 条已标注）。正在组建教研团队——" },
  {
    kind: "team",
    members: [
      ["课程内容教研员", "梳理本单元学习内容与要求"],
      ["学情数据分析师", "检查数据质量，分析个体与群体表现"],
      ["学习规划师", "依据薄弱点给出学习建议"],
      ["练习与测评设计", "匹配对应练习与难度"],
      ["质量审核", "核对统计口径与材料出处"],
    ],
  },
  {
    kind: "card",
    tone: "post",
    title: "学习内容与要求清单已整理",
    detail: "有理数运算 · 整式加减 · 数轴与相反数 · 共 12 条要求，全部标注材料出处",
  },
  {
    kind: "card",
    tone: "link",
    title: "班级学情分析完成",
    detail: "均分 78.4 · 数与式运算失分集中 · 3 名学生记录不足，已标注不能下结论",
  },
  {
    kind: "card",
    tone: "order",
    title: "个性化学习建议已生成",
    detail: "为 6 名薄弱学生各给出 2 周学习内容、练习与先后安排",
  },
  {
    kind: "card",
    tone: "courier",
    title: "练习与活动已匹配",
    detail: "对应 8 道基础题 + 4 道变式题，标注难度与建议用时",
  },
  {
    kind: "card",
    tone: "profit",
    title: "学情报告已生成",
    detail: "含个体与群体摘要、数据缺口说明与材料引用清单",
  },
  {
    kind: "done",
    text: "全流程跑通 ✅ 学习要求、学情分析与学习建议都已核对，可导出报告或继续调整。",
  },
];

const demoMetrics = [
  { key: "kb", label: "课程资料", value: "2 份", at: 2, icon: Database },
  { key: "team", label: "教研团队", value: "5 人", at: 4, icon: UsersRound },
  { key: "post", label: "内容清单", value: "12 条", at: 5, icon: Megaphone },
  { key: "link", label: "学情摘要", value: "1 份", at: 6, icon: Link2 },
  { key: "order", label: "学习建议", value: "6 人", at: 7, icon: ShoppingBag },
  { key: "ship", label: "练习匹配", value: "12 题", at: 8, icon: Truck },
  { key: "profit", label: "学情报告", value: "1 份", at: 9, icon: Wallet },
];

const cardIcons = { post: Megaphone, link: Link2, order: ShoppingBag, courier: Truck, profit: Wallet };
const cardRoles = { post: "课程内容教研员", link: "学情数据分析师", order: "学习规划师", courier: "练习与测评设计", profit: "质量审核" };

function renderDemoEvent(event: DemoEvent, index: number) {
  if (event.kind === "ceo") {
    return (
      <div key={index} className="sample-msg ceo">
        <span className="sample-msg-avatar">
          <BriefcaseBusiness size={15} />
        </span>
        <div>
          <small>教学主管</small>
          <p>{event.text}</p>
        </div>
      </div>
    );
  }
  if (event.kind === "user") {
    return (
      <div key={index} className="sample-msg user">
        <div>
          <small>老师 · 七年级 3 班</small>
          <p>{event.text}</p>
          <div className="sample-chips">
            {event.chips.map((chip) => (
              <span key={chip.label}>
                {chip.icon === "image" ? <ImageIcon size={11} /> : <FileText size={11} />}
                {chip.label}
              </span>
            ))}
          </div>
        </div>
      </div>
    );
  }
  if (event.kind === "note") {
    return (
      <div key={index} className="sample-note">
        <Database size={12} /> {event.text}
      </div>
    );
  }
  if (event.kind === "team") {
    return (
      <div key={index} className="sample-team-card">
        <p>
          <UsersRound size={13} /> 教研团队已组建 · 任务自动分发
        </p>
        <div>
          {event.members.map(([role, duty]) => (
            <span key={role}>
              <strong>{role}</strong>
              <small>{duty}</small>
            </span>
          ))}
        </div>
      </div>
    );
  }
  if (event.kind === "card") {
    const Icon = cardIcons[event.tone];
    return (
      <div key={index} className={`sample-agent-card ${event.tone}`}>
        <span className="sample-agent-icon">
          <Icon size={15} />
        </span>
        <div>
          <small>{cardRoles[event.tone]} · 已完成</small>
          <strong>{event.title}</strong>
          <p>{event.detail}</p>
        </div>
        <Check size={14} />
      </div>
    );
  }
  return (
    <div key={index} className="sample-done">
      <Check size={16} /> {event.text}
    </div>
  );
}

function SampleDemo() {
  const [step, setStep] = useState(0);
  const [playing, setPlaying] = useState(false);
  const streamRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!playing) return;
    if (step >= demoEvents.length) {
      setPlaying(false);
      return;
    }
    const timer = setTimeout(() => setStep((s) => s + 1), 1900);
    return () => clearTimeout(timer);
  }, [playing, step]);

  useEffect(() => {
    const el = streamRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [step]);

  function togglePlay() {
    if (step >= demoEvents.length) setStep(0);
    setPlaying((p) => !p);
  }

  return (
    <div className="sample-demo">
      <div className="sample-demo-bar">
        <span className="live-dot">DEMO</span>
        <strong>七年级 3 班 · 学情诊断</strong>
        <div className="sample-demo-controls">
          <button className="sample-demo-btn primary" onClick={togglePlay}>
            {playing ? <Pause size={13} /> : <Play size={13} fill="currentColor" />}
            {playing
              ? "暂停"
              : step >= demoEvents.length
                ? "重新播放"
                : step === 0
                  ? "播放演示"
                  : "继续播放"}
          </button>
          <button
            className="sample-demo-btn"
            disabled={step >= demoEvents.length}
            onClick={() => {
              setPlaying(false);
              setStep((s) => Math.min(s + 1, demoEvents.length));
            }}
          >
            下一步
          </button>
          <button
            className="sample-demo-btn"
            onClick={() => {
              setPlaying(false);
              setStep(0);
            }}
          >
            <RefreshCw size={12} /> 重置
          </button>
          <small>
            第 {step} / {demoEvents.length} 步
          </small>
        </div>
      </div>
      <div className="sample-demo-body">
        <div className="sample-chat" ref={streamRef}>
          {demoEvents.slice(0, step).map((event, index) => renderDemoEvent(event, index))}
          {step === 0 && <div className="sample-chat-hint">点击「播放演示」开始 →</div>}
        </div>
        <aside className="sample-board">
          <p className="sample-board-title">诊断进度实时同步</p>
          <div className="sample-board-grid">
            {demoMetrics.map((metric) => {
              const on = step > metric.at;
              const Icon = metric.icon;
              return (
                <div key={metric.key} className={on ? "on" : ""}>
                  <Icon size={15} />
                  <small>{metric.label}</small>
                  <strong>{on ? metric.value : "—"}</strong>
                </div>
              );
            })}
          </div>
          <div className="sample-board-foot">
            <Zap size={13} />{" "}
            {step >= demoEvents.length
              ? "全流程已跑通"
              : playing
                ? "Agent 团队执行中…"
                : step === 0
                  ? "待开始"
                  : "已暂停"}
          </div>
        </aside>
      </div>
    </div>
  );
}

export default LandingApp;
