/* 从 LLM 到 Harness · 21 页 PPTX 构建脚本（Tokyo Night 深色主题，全部原生可编辑元素） */
const pptxgen = require("pptxgenjs");
const OUT = "/workspace/llm-harness-share/llm-harness-share.pptx";

/* ===== 主题（Tokyo Night） ===== */
const BG="1A1B26", SURFACE="24283B", SURFACE2="2F334D", BORDER="343A54",
  T1="C0CAF5", T2="A9B1D6", T3="565F89",
  ACCENT="7AA2F7", ACCENT2="BB9AF7", GOOD="9ECE6A", WARM="E0AF68", BAD="F7768E";
const F = "Microsoft YaHei";

/* ===== 布局常量 LAYOUT_WIDE 13.333 x 7.5 ===== */
const SW=13.333, SH=7.5, MX=0.5, CW=SW-2*MX; // 12.333
const FOOT_Y=7.08;

const pres = new pptxgen();
pres.layout = "LAYOUT_WIDE";
pres.author = "智能问答平台项目组";
pres.title = "从 LLM 到 Harness · 企业级智能问答平台实践";

/* ===== helpers ===== */
function baseSlide(bgColor){ const s=pres.addSlide(); s.background={color:bgColor||BG}; return s; }
function kicker(s, str, opts={}){ s.addText(str,{x:MX,y:0.42,w:CW,h:0.3,fontFace:F,fontSize:11,bold:true,color:ACCENT,charSpacing:1.5,align:opts.align||"left",margin:0}); }
function h2(s, parts){ s.addText(parts,{x:MX,y:0.72,w:CW,h:0.62,fontFace:F,fontSize:27,bold:true,color:T1,align:"left",margin:0,valign:"middle"}); }
function footer(s, n, leftStr){
  s.addText(leftStr,{x:MX,y:FOOT_Y,w:8,h:0.28,fontFace:F,fontSize:8.5,color:T3,align:"left",margin:0,valign:"middle"});
  s.addText(n+" / 21",{x:SW-2.5-MX,y:FOOT_Y,w:2.5,h:0.28,fontFace:F,fontSize:8.5,color:T3,align:"right",margin:0,valign:"middle"});
}
function cjkW(str,fs){ let w=0; for(const ch of str){ w += ch.charCodeAt(0)>255 ? 1 : 0.6; } return w*fs/72+0.08; }
/* 图表页坐标映射器 */
function mapper(S,X0,Y0){ return { S, X:(v)=>X0+v*S, Y:(v)=>Y0+v*S }; }
function ctext(s,mx,cx,yBase,str,fs,color,opts={}){
  const w=opts.w||cjkW(str,fs), h=opts.h||fs*1.7/72;
  s.addText(str,Object.assign({x:mx.X(cx)-w/2,y:mx.Y(yBase)-h/2-fs*0.12/72,w,h,fontFace:F,fontSize:fs,color:color||T1,align:"center",valign:"middle",margin:0},opts.box||{}));
}
function ltext(s,mx,x,yBase,str,fs,color,wIn){
  const h=fs*1.7/72;
  const w=wIn||Math.max(0.4, SW-MX-mx.X(x)); // 默认取剩余宽度，防越界
  s.addText(str,{x:mx.X(x),y:mx.Y(yBase)-h/2-fs*0.12/72,w,h,fontFace:F,fontSize:fs,color:color||T1,align:"left",valign:"middle",margin:0});
}
function rtext(s,mx,xRight,yBase,str,fs,color,w){
  const ww=w||cjkW(str,fs), h=fs*1.7/72;
  s.addText(str,{x:mx.X(xRight)-ww,y:mx.Y(yBase)-h/2-fs*0.12/72,w:ww,h,fontFace:F,fontSize:fs,color:color||T1,align:"right",valign:"middle",margin:0});
}
function arrow(s,mx,x1,y1,x2,y2,o={}){
  const x=Math.min(x1,x2),y=Math.min(y1,y2),w=Math.abs(x2-x1)*mx.S,h=Math.abs(y2-y1)*mx.S;
  s.addShape("line",{x:mx.X(x),y:mx.Y(y),w,h,flipH:x2<x1,flipV:y2<y1,
    line:{color:o.color||ACCENT,width:o.width||1.4,dashType:o.dash?"dash":"solid",endArrowType:o.noArrow?undefined:"triangle"}});
}
function box(s,mx,x,y,w,h,o={}){
  s.addShape(o.shape||"roundRect",{x:mx.X(x),y:mx.Y(y),w:w*mx.S,h:h*mx.S,rectRadius:(o.r||10)*mx.S*(o.rIn?1:1),
    fill:o.fillNone?{type:"none"}:{color:o.fill||SURFACE,transparency:o.ft==null?0:o.ft},
    line:o.lineNone?{transparency:100}:{color:o.lineColor||BORDER,width:o.lw||0.75,dashType:o.dash?"dash":"solid"}});
  if(o.r) ; // rectRadius already in inches via *S approx
}
/* 模块卡（emoji+名称+副文，居中三行） */
function mod(s,mx,x,y,w,h,emoji,name,sub,o={}){
  box(s,mx,x,y,w,h,Object.assign({fill:SURFACE,lineColor:o.hl?ACCENT:BORDER,lw:o.hl?1.2:0.75,r:10},o));
  const parts=[{text:emoji,options:{fontSize:11,color:T1,breakLine:true}},{text:name,options:{fontSize:10,bold:true,color:T1,breakLine:true}},{text:sub,options:{fontSize:8,color:T3}}];
  s.addText(parts,{x:mx.X(x),y:mx.Y(y)+2*mx.S,w:w*mx.S,h:h*mx.S-4*mx.S,fontFace:F,align:"center",valign:"middle",margin:0,lineSpacingMultiple:1.05});
}
function notes(s,str){ s.addNotes(str); }

/* ============ P1 封面 ============ */
(()=>{ const s=baseSlide();
  s.addImage({path:"/workspace/llm-harness-share/assets/cover-bg.jpg",x:0,y:0,w:SW,h:SH,sizing:{type:"cover",w:SW,h:SH}});
  s.addShape("rect",{x:0,y:0,w:SW,h:SH,fill:{color:"050A19",transparency:42},line:{transparency:100}});
  s.addShape("rect",{x:0,y:4.0,w:SW,h:3.5,fill:{color:"050A19",transparency:22},line:{transparency:100}});
  s.addText("项目分享会 · 2026",{x:MX,y:0.45,w:6,h:0.3,fontFace:F,fontSize:11,bold:true,color:"C8CEE8",charSpacing:2,margin:0});
  s.addText("PROJECT SHARING",{x:SW-4.5-MX,y:0.45,w:4.5,h:0.3,fontFace:F,fontSize:11,bold:true,color:"9AA3C8",charSpacing:2,align:"right",margin:0});
  s.addText([
    {text:"从 LLM 到 ",options:{color:"FFFFFF"}},
    {text:"Harness",options:{color:ACCENT,breakLine:true}},
    {text:"企业级智能问答平台实践",options:{color:"FFFFFF"}}
  ],{x:MX,y:2.55,w:CW,h:1.7,fontFace:F,fontSize:40,bold:true,align:"left",valign:"top",margin:0,lineSpacingMultiple:1.12});
  s.addText("当一个「聪明的大脑」走进企业：我们如何用配置中心 + 后端运行时 + 前端组件，把它变成能干活的平台。",
    {x:MX,y:4.45,w:11.2,h:0.6,fontFace:F,fontSize:15,color:"D5DAF0",align:"left",margin:0});
  s.addText("智能问答平台项目组 · 2026-11",{x:MX,y:FOOT_Y,w:6,h:0.28,fontFace:F,fontSize:10,color:"A9B1D6",margin:0,valign:"middle"});
  s.addText("1 / 21",{x:SW-2.5-MX,y:FOOT_Y,w:2.5,h:0.28,fontFace:F,fontSize:10,color:"A9B1D6",align:"right",margin:0,valign:"middle"});
  notes(s,"开场：今天这场分享不讲代码细节，讲整体思路。三个部分：为什么需要 LLM 之后的 Agent 和 Harness；我们项目的架构与创新点；最后看一眼新一代的 DeepSeek Harness，对照我们的方向。");
})();

/* ============ P2 目录 ============ */
(()=>{ const s=baseSlide();
  kicker(s,"CONTENTS · 目录"); h2(s,[{text:"今天讲四件事",options:{}}]);
  const items=[
    ["01","技术演进：LLM → Agent → Harness","为什么单个 LLM 和 Agent 不足以支撑复杂任务。"],
    ["02","项目架构：配置中心 · 后端 · 前端","三层如何分工，把能力做成可运营的资产。"],
    ["03","创新点与实现思路","配置驱动编排 · 卡片反馈同轮恢复 · 沙箱技能工作区。"],
    ["04","新一代 Harness：DeepSeek Harness","「一切皆插件」的 Agent 运行时，与对我们的启示。"]];
  items.forEach((it,i)=>{
    const x=MX+(i%2)*(CW/2+0.08), y=1.65+Math.floor(i/2)*2.35, w=CW/2-0.08, h=2.15;
    s.addShape("roundRect",{x,y,w,h,rectRadius:0.12,fill:{color:SURFACE,transparency:20},line:{color:BORDER,width:0.75}});
    s.addText(it[0],{x:x+0.3,y:y+0.35,w:1.0,h:0.7,fontFace:F,fontSize:30,bold:true,color:T2,margin:0,valign:"top"});
    s.addText(it[1],{x:x+1.35,y:y+0.38,w:w-1.7,h:0.55,fontFace:F,fontSize:15.5,bold:true,color:T1,margin:0,valign:"top"});
    s.addText(it[2],{x:x+1.35,y:y+1.0,w:w-1.7,h:0.95,fontFace:F,fontSize:11.5,color:T2,margin:0,valign:"top",lineSpacingMultiple:1.3,fit:"shrink"});
  });
  footer(s,2,"从 LLM 到 Harness"); notes(s,"先交代技术背景，再进入项目本身，最后用 DeepSeek Harness 收尾并展望。整场约 25 分钟，欢迎随时打断提问。");
})();

/* ============ Section 页构建器 ============ */
function sectionSlide(no,line1,line2Arr,lede,note){
  const s=baseSlide();
  s.addShape("ellipse",{x:10.45,y:0.32,w:2.45,h:2.45,fill:{color:ACCENT,transparency:92},line:{transparency:100}});
  s.addShape("ellipse",{x:0.42,y:4.82,w:2.2,h:2.2,fill:{color:ACCENT2,transparency:93},line:{transparency:100}});
  s.addText("SECTION · 0"+no,{x:MX,y:2.0,w:CW,h:0.3,fontFace:F,fontSize:12,bold:true,color:ACCENT,charSpacing:2.5,align:"center",margin:0});
  s.addText(line1,{x:MX,y:2.45,w:CW,h:1.9,fontFace:F,fontSize:42,bold:true,color:"FFFFFF",align:"center",valign:"top",margin:0,lineSpacingMultiple:1.15});
  if(line2Arr&&line2Arr.length) s.addText(line2Arr,{x:MX,y:4.45,w:CW,h:0.75,fontFace:F,fontSize:42,bold:true,color:ACCENT,align:"center",valign:"top",margin:0});
  s.addShape("roundRect",{x:SW/2-1.1,y:(line2Arr&&line2Arr.length?5.45:4.75),w:2.2,h:0.05,rectRadius:0.02,fill:{color:ACCENT},line:{transparency:100}});
  s.addText(lede,{x:1.8,y:(line2Arr&&line2Arr.length?5.75:5.05),w:SW-3.6,h:0.5,fontFace:F,fontSize:14,color:T2,align:"center",margin:0});
  notes(s,note); return s;
}

/* ============ 三卡片页构建器 ============ */
function card3(kick,title,parts,ledeRich,footL,n,note){
  const s=baseSlide(); kicker(s,kick); h2(s,title);
  parts.forEach((p,i)=>{ const x=MX+i*(CW/3+0.04), y=1.68, w=CW/3-0.08, h=3.15;
    s.addShape("roundRect",{x,y,w,h,rectRadius:0.12,fill:{color:SURFACE,transparency:20},line:{color:BORDER,width:0.75}});
    s.addText(p[0],{x:x+0.28,y:y+0.25,w:1,h:0.6,fontFace:F,fontSize:22,color:T1,margin:0});
    s.addText(p[1],{x:x+0.28,y:y+0.95,w:w-0.56,h:0.42,fontFace:F,fontSize:15,bold:true,color:T1,margin:0,valign:"top"});
    s.addText(p[2],{x:x+0.28,y:y+1.45,w:w-0.56,h:h-1.7,fontFace:F,fontSize:11.5,color:T2,margin:0,valign:"top",lineSpacingMultiple:1.35,fit:"shrink"});
  });
  s.addText(ledeRich,{x:MX,y:5.25,w:CW,h:0.6,fontFace:F,fontSize:13.5,color:T1,align:"left",margin:0,valign:"top"});
  footer(s,n,footL); notes(s,note); return s;
}

/* ============ 双卡片页构建器 ============ */
function card2(kick,title,left,right,footL,n,note){
  const s=baseSlide(); kicker(s,kick); h2(s,title);
  [[MX,left],[MX+CW/2+0.08,right]].forEach(([x,c])=>{ const y=1.68,w=CW/2-0.08,h=4.35;
    s.addShape("roundRect",{x,y,w,h,rectRadius:0.12,fill:{color:SURFACE,transparency:20},line:{color:BORDER,width:0.75}});
    s.addText(c[0],{x:x+0.32,y:y+0.3,w:w-0.64,h:0.45,fontFace:F,fontSize:16,bold:true,color:T1,margin:0,valign:"top"});
    s.addText(c[1],{x:x+0.32,y:y+0.9,w:w-0.64,h:0.75,fontFace:F,fontSize:11.5,color:T2,margin:0,valign:"top",lineSpacingMultiple:1.3,fit:"shrink"});
    const rows=c[2].map(t=>({text:t,options:{breakLine:true}}));
    s.addText(rows,{x:x+0.32,y:y+1.8,w:w-0.64,h:h-2.1,fontFace:F,fontSize:12,color:T2,margin:0,valign:"top",lineSpacingMultiple:1.62});
  });
  footer(s,n,footL); notes(s,note); return s;
}

/* P3 */
sectionSlide(1,"技术演进：",[{text:"LLM → Agent → ",options:{color:"FFFFFF"}},{text:"Harness",options:{color:ACCENT}}],
  "模型越来越聪明，但「聪明的模型」不等于「能干活的系统」。",1,
  "第一部分回答一个问题：为什么行业从单模型，走到 Agent，又走到 Harness。这是理解我们项目设计的前提。");
/* P4 */
card3("LIMIT · 单模型的边界",[{text:"单个 LLM：聪明的大脑，",options:{color:T1}},{text:"无力的手脚",options:{color:T3}}],
  [["🧊","知识是冻结的","训练数据有截止日期。今日新闻、实时行情、公司内部制度——模型一概不知道，也查不到。"],
   ["🤐","只会说，不会做","输出只能是文本。不能联网搜索、不能查知识库、不能渲染图表、更不能执行有副作用的操作。"],
   ["🌀","没有状态与规划","上下文窗口有限，长链条任务容易跑偏、遗忘中间结论；没有权限边界，企业场景无法直接上。"]],
  [{text:"复杂任务需要的不止「会思考」，还要",options:{color:T1}},{text:"「能行动、有记忆、守规矩」",options:{bold:true,color:ACCENT}},{text:"。",options:{color:T1}}],
  "Part 01 · 技术演进",4,
  "企业里的真实任务：查实时数据、查内部制度、画图、让用户确认再执行——单模型一个都做不了。于是有了 Agent。");
/* P5 */
card2("EVOLUTION · Agent 时代",[{text:"Agent：给模型装上工具，",options:{color:T1}},{text:"但工程设施各自为政",options:{color:T3}}],
  ["Agent = LLM + 工具 + ReAct 循环","模型在「思考 → 调用工具 → 观察结果 → 再思考」的循环里完成任务，能力大幅扩展。",
   ["— 接搜索：解决实时信息","— 接知识库：解决私域知识","— 接渲染工具：把答案变成图表、卡片","— 我们的问答平台，核心也是一组 Agent"]],
  ["但复杂任务下，痛点浮现","",
   ["— 模型接入、会话管理、沙箱、持久化、UI……每个项目重复造轮子","— 能力与运行时耦合：换模型、加工具都要动代码","— 缺统一权限、隔离与可观测性，企业级落地难","— 单 Agent 单循环，难以支撑多意图、多智能体协作"]],
  "Part 01 · 技术演进",5,
  "Agent 解决了「会用工具」，但把这些能力组装成一个可靠系统的工程层——谁来管会话、管沙箱、管权限、管渲染？——仍然各写各的。这一层就叫 Harness。");
/* P6 Harness 定义 */
(()=>{ const s=baseSlide();
  s.addShape("ellipse",{x:10.55,y:0.32,w:2.35,h:2.35,fill:{color:ACCENT,transparency:93},line:{transparency:100}});
  kicker(s,"THE ANSWER · 运行时层");
  s.addText([{text:"Model + ",options:{color:"FFFFFF"}},{text:"Harness",options:{color:ACCENT}},{text:" = Agent",options:{color:"FFFFFF"}}],
    {x:MX,y:1.95,w:CW,h:1.05,fontFace:F,fontSize:40,bold:true,align:"center",valign:"middle",margin:0,charSpacing:1});
  s.addText("模型负责「思考」，Harness 负责「执行」——把模型之上的工程设施标准化。",
    {x:1.8,y:3.1,w:SW-3.6,h:0.5,fontFace:F,fontSize:14,color:T2,align:"center",margin:0});
  const chips=[["模型接入","可替换"],["工具调度","技能化"],["会话状态","可恢复"],["沙箱隔离","安全执行"],["权限管控","企业级"],["组件渲染","富交互"],["可观测","全链路"]];
  chips.forEach((c,i)=>{
    const row=i<4?0:1, perRow=row===0?4:3, idx=row===0?i:i-4;
    const w=2.42, gap=0.32, total=row===0?perRow*w+(perRow-1)*gap:perRow*w+(perRow-1)*gap;
    const x0=SW/2-total/2, y=row===0?4.35:5.15;
    s.addShape("roundRect",{x:x0+idx*(w+gap),y,w,h:0.52,rectRadius:0.26,fill:{color:SURFACE},line:{color:BORDER,width:0.75}});
    s.addText([{text:c[0],options:{bold:true,color:T1}},{text:" · "+c[1],options:{color:T2}}],
      {x:x0+idx*(w+gap),y,w,h:0.52,fontFace:F,fontSize:12,align:"center",valign:"middle",margin:0});
  });
  notes(s,"一句话给 Harness 下定义：模型之上、应用之下的那层运行时。接下来介绍的我们这个项目，本质上就是在企业场景里自建了一层 Harness。");
})();
/* P7 */
sectionSlide(2,"项目架构：",[{text:"配置中心 · ",options:{color:"FFFFFF"}},{text:"后端",options:{color:ACCENT}},{text:" · 前端",options:{color:"FFFFFF"}}],
  "能力放在配置里，执行放在运行时里，体验放在终端里。",2,
  "项目是一个企业级智能问答平台：多智能体、可配置、带交互卡片。整体分三块：管理侧的配置中心、FastAPI 后端运行时、前端对话终端。");

/* ============ P8 总体架构（三层架构图 · 原生形状） ============ */
(()=>{ const s=baseSlide();
  kicker(s,"ARCHITECTURE · 系统总览");
  h2(s,[{text:"三层架构：能力配置 → 编排执行 → 交互呈现",options:{}}]);
  const mx=mapper(12.333/1760,0.5,1.42);
  /* 层容器 */
  box(s,mx,270,10,1220,160,{fill:SURFACE,ft:60,r:14});
  box(s,mx,270,258,1220,160,{fill:SURFACE,ft:60,lineColor:ACCENT,lw:1.2,r:14});
  box(s,mx,270,506,1220,160,{fill:SURFACE,ft:60,r:14});
  /* 左侧层标签 */
  [["🖥️","前端终端","交互呈现",10],["⚙️","后端运行时","编排执行",258],["🎛️","配置中心","管理端 mng",506]].forEach(L=>{
    box(s,mx,84,L[3],160,160,{fill:SURFACE2,r:12});
    s.addText([{text:L[0],options:{fontSize:13,breakLine:true}},{text:L[1],options:{fontSize:10.5,bold:true,breakLine:true}},{text:L[2],options:{fontSize:8,color:T3}}],
      {x:mx.X(84),y:mx.Y(L[3])+14*mx.S,w:160*mx.S,h:160*mx.S-28*mx.S,fontFace:F,color:T1,align:"center",valign:"middle",margin:0,lineSpacingMultiple:1.15});
  });
  /* 12 模块 */
  const rows=[38,286,534];
  const mods=[
    [["💬","流式对话","SSE 逐字输出"],["📊","组件渲染","图表 · 指标卡 · 卡片"],["👆","反馈回传","/chat/feedback"],["🗂️","会话管理","历史 · 收藏 · 上传"]],
    [["🧭","意图编排","识别 → 改写 → 路由"],["🤖","多智能体","按技能组装工具箱"],["📦","沙箱工作区","OpenSandbox 隔离"],["🛠️","工具体系","搜索·知识库·渲染·反馈"]],
    [["🎯","意图配置","意图 → 智能体映射"],["🤖","智能体配置","技能子集 + 提示词"],["🧩","技能注册","目录 → 沙箱注入"],["🔐","权限 · 卡片","白名单 · 模板 schema"]]];
  mods.forEach((rowArr,ri)=>rowArr.forEach((mm,ci)=>{ mod(s,mx,294+ci*300,rows[ri],272,96,mm[0],mm[1],mm[2],ri===1?{hl:true}:{}); }));
  /* 数据流箭头 + 标签 */
  arrow(s,mx,560,250,560,180);
  rtext(s,mx,545,204,"流式回答 + 组件协议",9,ACCENT); rtext(s,mx,545,222,"SSE 文本流 · 组件流",8,T3);
  arrow(s,mx,900,180,900,250);
  ltext(s,mx,915,204,"提问 / 反馈",9,ACCENT); ltext(s,mx,915,222,"POST /chat · /chat/feedback",8,T3);
  arrow(s,mx,560,498,560,428);
  rtext(s,mx,545,452,"登录时融合配置",9,ACCENT); rtext(s,mx,545,470,"意图 / 智能体 / 技能 / 权限",8,T3);
  arrow(s,mx,900,428,900,498,{color:WARM,width:1.2,dash:true});
  ltext(s,mx,915,452,"每轮读取配置缓存",9,WARM); ltext(s,mx,915,470,"免重复融合 · 快速路由",8,T3);
  /* 右侧：配置 → 前端 模板下发（折线，端点贴合层容器右缘） */
  arrow(s,mx,1490,586,1660,586,{color:WARM,width:1.2,dash:true,noArrow:true});
  arrow(s,mx,1660,586,1660,90,{color:WARM,width:1.2,dash:true,noArrow:true});
  arrow(s,mx,1660,90,1490,90,{color:WARM,width:1.2,dash:true});
  const rw=cjkW("登录时下发：卡片模板 · 可见智能体列表",8)+0.15;
  s.addText("登录时下发：卡片模板 · 可见智能体列表",{x:mx.X(1620)-rw/2,y:mx.Y(340)-rw/2,w:rw,h:0.26,fontFace:F,fontSize:8,color:T3,align:"center",valign:"middle",margin:0,rotate:90});
  footer(s,8,"Part 02 · 项目架构");
  notes(s,"从下往上看：配置中心定义「有哪些能力、谁能用」；后端负责「怎么组合与执行」；前端负责「怎么呈现与交互」。虚线下发、运行时消费。");
})();

/* ============ P9 配置中心（生命周期图） ============ */
(()=>{ const s=baseSlide();
  kicker(s,"CONFIG · 配置中心");
  h2(s,[{text:"配置中心：一切能力皆可配置",options:{}}]);
  const mx=mapper(12.333/1760,0.5,1.40);
  /* ① 生产 */
  box(s,mx,30,20,480,360,{fill:SURFACE,ft:60,r:14});
  ltext(s,mx,54,52,"① 生产 · 谁定义能力",9,T2);
  box(s,mx,54,76,432,130,{fill:SURFACE,r:10});
  s.addText([{text:"🏢 管理端 mng · 运营视角",options:{fontSize:10.5,bold:true,color:T1,breakLine:true}},
    {text:"· 意图 → 智能体映射 · 卡片模板 schemaFields",options:{fontSize:8.5,color:T2,breakLine:true}},
    {text:"· 智能体技能绑定与提示词 · 用户 / 智能体权限",options:{fontSize:8.5,color:T2}}],
    {x:mx.X(74),y:mx.Y(88),w:392*mx.S,h:106*mx.S,fontFace:F,align:"left",valign:"middle",margin:0,lineSpacingMultiple:1.35});
  box(s,mx,54,224,432,130,{fill:SURFACE,r:10});
  s.addText([{text:"📝 本地 YAML · 工程视角",options:{fontSize:10.5,bold:true,color:T1,breakLine:true}},
    {text:"· 技能目录（SKILL.md + tools.py）",options:{fontSize:8.5,color:T2,breakLine:true}},
    {text:"· 工具注册与卡片类型常量",options:{fontSize:8.5,color:T2,breakLine:true}},
    {text:"随代码仓版本化 · Code Review 把关",options:{fontSize:8,color:T3}}],
    {x:mx.X(74),y:mx.Y(236),w:392*mx.S,h:106*mx.S,fontFace:F,align:"left",valign:"middle",margin:0,lineSpacingMultiple:1.3});
  /* ② 融合 */
  box(s,mx,570,20,290,360,{fill:SURFACE,ft:60,lineColor:ACCENT,lw:1.2,r:14});
  ltext(s,mx,594,52,"② 融合 · 登录时一次",9,T2);
  s.addShape("can",{x:mx.X(620),y:mx.Y(150),w:190*mx.S,h:152*mx.S,fill:{color:SURFACE},line:{color:BORDER,width:1}});
  ctext(s,mx,715,222,"Redis",11,T1,{box:{bold:true}});
  ctext(s,mx,715,248,"用户级配置缓存",8,T3);
  ctext(s,mx,715,330,"mng 配置 + YAML",8,T3);
  ctext(s,mx,715,350,"按用户权限融合",8,T3);
  /* ③ 消费 */
  box(s,mx,920,20,810,360,{fill:SURFACE,ft:60,r:14});
  ltext(s,mx,944,52,"③ 消费 · 谁在使用",9,T2);
  [["⚙️","后端编排运行时","每轮对话：读意图 → 路由智能体 → 按技能子集组装工具箱",76],
   ["📦","沙箱工作区","首次对话：注入技能文件 → /workspace/skills · 沙箱存活期内复用",182],
   ["🖥️","前端终端","登录后加载卡片模板 · 按 cardType + schemaFields 渲染组件",288]].forEach(c=>{
    box(s,mx,944,c[3],762,88,{fill:SURFACE,r:10});
    s.addText([{text:c[0]+" "+c[1],options:{fontSize:10.5,bold:true,color:T1,breakLine:true}},{text:c[2],options:{fontSize:8.5,color:T2}}],
      {x:mx.X(968),y:mx.Y(c[3])+12*mx.S,w:714*mx.S,h:64*mx.S,fontFace:F,align:"left",valign:"middle",margin:0,lineSpacingMultiple:1.35});
  });
  /* 流向箭头 */
  arrow(s,mx,510,200,560,200); arrow(s,mx,860,200,912,200);
  /* ④ 变更生效 */
  box(s,mx,30,410,1700,256,{fill:SURFACE,ft:60,r:14});
  ltext(s,mx,54,442,"④ 变更如何生效 · 不改代码、免发版",9,T2);
  box(s,mx,60,470,190,176,{fill:SURFACE,lineColor:ACCENT,lw:1.2,r:12});
  s.addText([{text:"⚙️",options:{fontSize:13,breakLine:true}},{text:"配置变更",options:{fontSize:10.5,bold:true,breakLine:true}},{text:"管理端 / YAML",options:{fontSize:8,color:T3}}],
    {x:mx.X(60),y:mx.Y(470)+16*mx.S,w:190*mx.S,h:176*mx.S-32*mx.S,fontFace:F,color:T1,align:"center",valign:"middle",margin:0,lineSpacingMultiple:1.2});
  arrow(s,mx,254,497,554,497); arrow(s,mx,254,564,554,564); arrow(s,mx,254,631,554,631);
  [["🔄 重新登录 → 刷新 Redis 配置缓存 · 意图 / 提示词 / 权限即刻生效",470],
   ["⏳ 沙箱 TTL 过期 → 下次对话重建沙箱 · 注入最新版技能文件",537],
   ["🎨 卡片模板更新 → 前端下次加载新模板 · 新卡片类型零前端发版",604]].forEach(r=>{
    box(s,mx,560,r[1],1140,54,{fill:SURFACE,r:10});
    ltext(s,mx,584,r[1]+30,r[0],8.5,T2);
  });
  s.addText("配置之外还有一层用户权限：登录时与管理端融合，决定每个用户能见哪些智能体、哪些技能。",
    {x:MX,y:6.42,w:CW,h:0.5,fontFace:F,fontSize:12.5,color:T2,align:"left",margin:0,valign:"top"});
  footer(s,9,"Part 02 · 项目架构");
  notes(s,"关键理念：能力不是写死在代码里的，是登记在配置里的。新增一个「制度问答智能体」？加一段 YAML 即可，下一页的创新点会展开。");
})();

/* P10 后端 */
card2("RUNTIME · 后端",[{text:"后端：编排引擎 + 沙箱工作区",options:{}}],
  ["🧭 编排链路","",
   ["— 登录时融合本地配置与管理端权限，缓存 Redis","— 每轮对话：改写 → 意图识别 → 路由到智能体","— 单意图直接执行；多意图按关系并行 / 流水线 / ReAct","— 意图识别与工作区装配并行进行，压低首字延迟"]],
  ["📦 沙箱与工具","",
   ["— OpenSandbox：按用户分配沙箱，按会话隔离目录","— 技能文件在首次对话时注入沙箱，随取随用","— 工具工厂闭包注入会话/用户上下文（搜索、知识库、卡片、反馈）","— 每个智能体只拿到自己技能子集对应的工具箱"]],
  "Part 02 · 项目架构",10,
  "后端就是我们的「迷你 Harness」：模型之上，把编排、隔离、工具、状态管理全部收口。技术栈 FastAPI + AgentScope + OpenSandbox。");

/* P11 前端（三条横卡） */
(()=>{ const s=baseSlide();
  kicker(s,"TERMINAL · 前端");
  h2(s,[{text:"前端：流式对话 + 组件化卡片",options:{}}]);
  [["① 流式体验","回答逐字流出，工具调用过程实时可见——用户等得住，因为看得见它在干活。"],
   ["② 组件化渲染","后端产出统一组件协议：折线图、指标卡、自定义卡片（cardType + schema）。前端按配置中心的模板渲染，新增卡片类型前端零改动。"],
   ["③ 闭环交互","确认卡片上用户点一下 → 回传 feedback → 唤醒挂起中的智能体，同一轮对话继续往下走。"]].forEach((c,i)=>{
    const y=1.68+i*1.72;
    s.addShape("roundRect",{x:MX,y,w:CW,h:1.52,rectRadius:0.1,fill:{color:SURFACE,transparency:20},line:{color:BORDER,width:0.75}});
    s.addShape("rect",{x:MX,y:y+0.18,w:0.055,h:1.52-0.36,fill:{color:ACCENT},line:{transparency:100}});
    s.addText(c[0],{x:MX+0.35,y:y+0.18,w:3.4,h:0.45,fontFace:F,fontSize:15,bold:true,color:T1,margin:0,valign:"top"});
    s.addText(c[1],{x:MX+0.35,y:y+0.68,w:CW-0.8,h:0.72,fontFace:F,fontSize:11.5,color:T2,margin:0,valign:"top",lineSpacingMultiple:1.3,fit:"shrink"});
  });
  footer(s,11,"Part 02 · 项目架构");
  notes(s,"前端的重点不是页面，是协议：文本流 + 组件流 + 反馈通道，三件事撑起「能看、能点、能续」的对话体验。");
})();

/* P12 */
sectionSlide(3,"创新点",[{text:"与",options:{color:"FFFFFF"}},{text:"实现思路",options:{color:ACCENT}}],
  "三个创新点，一条主链路。",3,
  "三个创新点按「静态 → 动态 → 隔离」展开，最后用一次对话的完整链路把它们串起来。");

/* ============ vs 对比页构建器 ============ */
function vsSlide(kick,title,left,right,ledeRich,footL,n,note){
  const s=baseSlide(); kicker(s,kick); h2(s,title);
  const lw=5.45, y=1.62, h=3.9;
  [[MX,left],[6.85,right]].forEach(([x,c])=>{
    s.addShape("roundRect",{x,y,w:lw,h,rectRadius:0.12,fill:{color:SURFACE,transparency:20},line:{color:BORDER,width:0.75}});
    s.addShape("rect",{x,y,w:lw,h:0.045,fill:{color:c[2]},line:{transparency:100}});
    s.addText(c[0],{x:x+0.32,y:y+0.28,w:lw-0.64,h:0.45,fontFace:F,fontSize:16,bold:true,color:T1,margin:0,valign:"top"});
    const rows=c[1].map(t=>({text:t,options:{breakLine:true}}));
    s.addText(rows,{x:x+0.32,y:y+0.95,w:lw-0.64,h:h-1.25,fontFace:F,fontSize:12,color:T2,margin:0,valign:"top",lineSpacingMultiple:1.55});
  });
  s.addText("→",{x:6.05,y:3.15,w:0.75,h:0.7,fontFace:F,fontSize:30,bold:true,color:T3,align:"center",valign:"middle",margin:0});
  s.addText(ledeRich,{x:MX,y:5.8,w:CW,h:0.6,fontFace:F,fontSize:13.5,color:T1,align:"left",margin:0,valign:"top"});
  footer(s,n,footL); notes(s,note); return s;
}
/* P13 创新点一 */
vsSlide("INNOVATION · 01",[{text:"配置驱动的多智能体编排",options:{}}],
  ["📉 传统做法",["新增智能体 = 写代码、改路由、重新发版","工具对所有智能体全局暴露，职责不清","提示词散落在代码里，运营无法触碰"],BAD],
  ["📈 本项目",[["YAML 声明式注册：新增智能体零代码、免发版"],"每个智能体只见自己绑定的技能子集，天然隔离","意图 / 提示词 / 技能全部配置化，管理端可运营"],GOOD],
  [{text:"上线「制度问答」「深度调研」等新智能体，从排期开发变成",options:{color:T1}},{text:"改配置 + 发卡片",options:{bold:true,color:ACCENT}},{text:"。",options:{color:T1}}],
  "Part 03 · 创新点",13,
  "举例：上个月新增 feedback_confirm 技能并绑定到通用问答智能体，只加了两个文件和两行 YAML。注册表机制在运行时按技能名筛选装配工具箱。");

/* ============ P14 创新点二（四泳道时序图） ============ */
(()=>{ const s=baseSlide();
  kicker(s,"INNOVATION · 02");
  h2(s,[{text:"卡片反馈：同轮暂停-恢复",options:{}}]);
  const mx=mapper(12.333/1760,0.5,1.42);
  /* 泳道头 */
  [[205,"🤖 智能体"],[585,"🛠️ 工具层"],[935,"🖥️ 前端"],[1335,"👤 用户"]].forEach(hd=>{
    box(s,mx,hd[0],8,200,48,{fill:SURFACE2,r:10});
    ctext(s,mx,hd[0]+100,38,hd[1],10,T1,{box:{bold:true}});
  });
  /* 生命线 */
  [305,685,1035,1435].forEach(x=>{ s.addShape("line",{x:mx.X(x),y:mx.Y(56),w:0,h:(580-56)*mx.S,line:{color:BORDER,width:0.75,dashType:"dash"}}); });
  /* 激活条（挂起期间中断，分两段） */
  s.addShape("roundRect",{x:mx.X(680),y:mx.Y(92),w:10*mx.S,h:102*mx.S,rectRadius:0.03,fill:{color:ACCENT,transparency:78},line:{transparency:100}});
  s.addShape("roundRect",{x:mx.X(680),y:mx.Y(362),w:10*mx.S,h:126*mx.S,rectRadius:0.03,fill:{color:ACCENT,transparency:78},line:{transparency:100}});
  /* 消息 */
  arrow(s,mx,310,92,676,92);   ctext(s,mx,493,80,"① 调用 render_feedback_card（card_type + schema）",9,ACCENT);
  arrow(s,mx,690,132,1026,132); ctext(s,mx,858,120,"② 下发卡片组件（携带 feedbackId）",9,ACCENT);
  arrow(s,mx,1040,172,1426,172); ctext(s,mx,1233,160,"③ 渲染标题 + 副标题 + 两个选项按钮",9,ACCENT);
  /* 挂起等待框 */
  box(s,mx,565,200,240,156,{fillNone:true,lineColor:ACCENT,dash:true,lw:1.1,r:12});
  s.addText([{text:"⏸",options:{fontSize:14,breakLine:true}},{text:"工具挂起等待",options:{fontSize:10.5,bold:true,breakLine:true}},
    {text:"300s 超时 · 单会话单槽",options:{fontSize:8,color:T3,breakLine:true}},{text:"feedbackId 对账防错配",options:{fontSize:8,color:T3}}],
    {x:mx.X(565),y:mx.Y(200)+10*mx.S,w:240*mx.S,h:136*mx.S,fontFace:F,color:T1,align:"center",valign:"middle",margin:0,lineSpacingMultiple:1.25});
  arrow(s,mx,1430,386,1044,386); ctext(s,mx,1237,374,"④ 点击「选项A」",9,ACCENT);
  arrow(s,mx,1030,428,699,428); ctext(s,mx,865,416,"⑤ POST /chat/feedback：{action, payload}",9,ACCENT);
  arrow(s,mx,676,470,314,470,{color:GOOD,width:1.6}); ctext(s,mx,495,456,"⑥ 唤醒挂起 · 返回用户选择结果",9,GOOD);
  /* ⑦ 自环（三段折线） */
  arrow(s,mx,305,496,258,496,{noArrow:true});
  arrow(s,mx,258,496,258,528,{noArrow:true});
  arrow(s,mx,258,528,300,528);
  ltext(s,mx,380,518,"⑦ 同轮继续推理 · 不重复发问",9,ACCENT);
  arrow(s,mx,310,566,1426,566); ctext(s,mx,868,554,"⑧ 最终回答流式下发（基于用户的选择）",9,ACCENT);
  /* 异常分支注解条 */
  box(s,mx,30,596,1700,48,{fill:SURFACE2,r:10});
  ctext(s,mx,880,620,"异常分支 → 超时：友好提示重新发起 · 取消：终止后续流程 · 会话中断：流 finally 自动 cancel 联动清理挂起槽",8,T3);
  s.addText([{text:"对比传统「文字追问 → 下一轮再答」：",options:{color:T1}},{text:"体验从两轮对话压缩为一轮",options:{bold:true,color:ACCENT}},{text:"。",options:{color:T1}}],
    {x:MX,y:6.45,w:CW,h:0.42,fontFace:F,fontSize:13,align:"left",margin:0,valign:"top"});
  footer(s,14,"Part 03 · 创新点");
  notes(s,"这是项目里最有「运行时」味道的设计：工具执行中途可以挂起等待人类输入，恢复后仍在同一个 ReAct 循环里。人机协同的最小闭环。");
})();

/* P15 创新点三 */
card3("INNOVATION · 03",[{text:"沙箱隔离的技能工作区",options:{}}],
  [["👤","按用户分配沙箱","用户之间完全隔离；沙箱可复用，TTL 淘汰 + 崩溃自动重建，高可用有兜底。"],
   ["📁","按会话隔离目录","同一用户的多会话互不干扰；文件产出、上传解析各归各的目录。"],
   ["🧬","技能按需注入","技能文件在首次对话时写入沙箱；智能体只能在自己工作区内读写，越界即拒绝。"]],
  [{text:"安全模型很朴素：模型生成的任何内容都",options:{color:T1}},{text:"先当不可信代码对待",options:{bold:true,color:ACCENT}},{text:"，跑在隔离边界内。",options:{color:T1}}],
  "Part 03 · 创新点",15,
  "技能注入是懒加载的：登录只做配置融合，第一次问答才真正建沙箱、注入技能文件。沙箱存活期内技能不变，TTL 过期后下次对话自动刷新到最新版。");

/* ============ P16 完整链路（五泳道时序图） ============ */
(()=>{ const s=baseSlide();
  kicker(s,"WALKTHROUGH · 实现思路");
  h2(s,[{text:"一次对话的完整链路",options:{}}]);
  const mx=mapper(12.333/1760,0.5,1.42);
  [[65,"👤 用户"],[365,"🖥️ 前端终端"],[665,"🧭 后端编排"],[965,"🗄️ 配置 · Redis"],[1265,"📦 沙箱 · 工具"]].forEach(hd=>{
    box(s,mx,hd[0],8,230,48,{fill:SURFACE2,r:10});
    ctext(s,mx,hd[0]+115,38,hd[1],10,T1,{box:{bold:true}});
  });
  [180,480,780,1080,1380].forEach(x=>{ s.addShape("line",{x:mx.X(x),y:mx.Y(56),w:0,h:(596-56)*mx.S,line:{color:BORDER,width:0.75,dashType:"dash"}}); });
  /* 登录阶段 */
  arrow(s,mx,185,92,476,92);   ctext(s,mx,330,80,"① 登录",9,ACCENT);
  arrow(s,mx,485,126,776,126); ctext(s,mx,630,114,"② 认证请求",9,ACCENT);
  arrow(s,mx,785,160,1076,160); ctext(s,mx,930,148,"③ 融合：mng 配置 + YAML + 用户权限",9,ACCENT);
  /* ④ 自写（右侧自环） */
  arrow(s,mx,1080,186,1140,186,{noArrow:true});
  arrow(s,mx,1140,186,1140,214,{noArrow:true});
  arrow(s,mx,1140,214,1090,214);
  ltext(s,mx,1150,206,"④ 写入 Redis 用户级缓存",9,ACCENT);
  /* 对话阶段 */
  arrow(s,mx,185,248,476,248); ctext(s,mx,330,236,"⑤ 输入问题",9,ACCENT);
  arrow(s,mx,485,282,776,282); ctext(s,mx,630,270,"⑥ POST /chat · 建立 SSE 流",9,ACCENT);
  /* 并行区 */
  box(s,mx,700,306,830,146,{fillNone:true,lineColor:T3,dash:true,lw:1,r:12});
  ltext(s,mx,1330,328,"⑦ 与意图识别并行执行",8,T3,1.35);
  arrow(s,mx,785,356,1076,356); ctext(s,mx,928,344,"7a 意图识别（LLM 改写 + 分类）· 查映射",9,ACCENT);
  arrow(s,mx,785,420,1376,420); ctext(s,mx,1080,408,"7b 取用 / 新建沙箱 · 注入技能 · 组装工具箱",9,ACCENT);
  /* ⑧ ReAct 自环（左侧） */
  arrow(s,mx,780,478,722,478,{noArrow:true});
  arrow(s,mx,722,478,722,506,{noArrow:true});
  arrow(s,mx,722,506,776,506);
  ltext(s,mx,850,498,"⑧ ReAct 循环：思考 → 调用工具 → 观察",9,ACCENT);
  arrow(s,mx,775,540,484,540); ctext(s,mx,630,528,"⑨ 流式回答 + 组件协议（SSE）",9,ACCENT);
  arrow(s,mx,475,574,184,574); ctext(s,mx,330,562,"⑩ 渲染图表 / 交互卡片",9,ACCENT);
  /* 交互分支条 */
  box(s,mx,180,602,1150,54,{fillNone:true,lineColor:WARM,dash:true,lw:1.1,r:10});
  ctext(s,mx,755,629,"交互分支：卡片挂起 → 用户点击 → POST /chat/feedback → 同轮恢复（详见前页时序图）",8,WARM);
  /* 完成条 */
  box(s,mx,30,682,1700,48,{fill:SURFACE2,r:10});
  ctext(s,mx,880,706,"回答完成 · 会话与产物落库 · 沙箱存活期内复用，TTL 过期自动淘汰",8,T3);
  footer(s,16,"Part 03 · 实现思路");
  notes(s,"串起来讲：配置决定能力边界，意图决定谁来干活，沙箱决定在哪干活，ReAct 决定怎么干活，组件协议决定怎么呈现。五个环节里 2 和 3 是并行的。");
})();

/* P17 Section 04 */
sectionSlide(4,"新一代 Harness：",[{text:"DeepSeek Harness",options:{color:ACCENT}}],
  "「一切皆插件」的开源 Agent 运行时（2026.08 · MIT）。",4,
  "最后把视野拉出去：我们自建的这层 Harness，业界已经出现了标准化的开源实现。看看 DeepSeek Harness 交出的答卷。");

/* P18 DeepSeek Harness 定位 */
card2("LANDSCAPE · 定位",[{text:"DeepSeek Harness：模型之上的开源运行时",options:{}}],
  ["它是什么","",
   [[{text:"— 不是又一个大模型，是 ",options:{color:T2}},{text:"Model + Harness = Agent",options:{bold:true,color:T1}},{text:" 的官方实践",options:{color:T2,breakLine:true}}],
    "— 底座 Cordis：一个只管「插件加载 / 卸载 / 依赖管理」的元框架",
    "— 源自 Koishi 作者，经跨平台、海量插件生产验证后整体并入",
    "— 全链路可追溯：模型看到的每个 token 都有记录"]],
  ["「一切皆插件」","",
   [[{text:"— 模型、工具、技能、会话、沙箱、存储、循环、调度、",options:{color:T2}},{text:"连 UI",options:{bold:true,color:T1}},{text:" 都是插件",options:{color:T2,breakLine:true}}],
    "— 配置即架构：改配置即热替换，不改源码、无需重启",
    "— 四种运行模式：标准 / PTC 编程式工具调用 / 极简 / 创意"]],
  "Part 04 · DeepSeek Harness",18,
  "和我们自建的思路同源，但更彻底：我们配置化的是「能力」，它把「运行时本身」也插件化了。PTC 模式值得一提——让模型写代码来编排多轮工具调用，降低长链路往返成本。");

/* P19 三大核心机制 */
card3("MECHANISMS · 核心机制",[{text:"撑起「一切皆插件」的三大机制",options:{}}],
  [["♻️","可逆副作用","任何注册动作都必须登记撤销函数，卸载时 LIFO 逆序清理。失败原子性、级联清理、运行时热插拔由此成立。"],
   ["🔌","服务与事件","能力消费走服务、状态广播走事件，双通道松耦合；作用域链天然隔离——子层可见父层，反之不可见。"],
   ["📦","预设与作用域","全局—预设—会话的作用域链；一份插件树多会话复用，工具可见性按作用域运行时遮蔽，不再全局二元。"]],
  [{text:"设计哲学一句话：",options:{color:T1}},{text:"把「卸载」和「加载」放到同等地位",options:{bold:true,color:ACCENT}},{text:"，热插拔才不是口号。",options:{color:T1}}],
  "Part 04 · DeepSeek Harness",19,
  "重点讲可逆副作用：传统 DI 容器只管绑定不管解绑，插件一多就幽灵回调、连接泄漏。Cordis 把清理变成框架的结构性保证。安全上 Code Mode 用 worker_threads 而非 vm 沙箱，隔离边界内不信任任何模型输入——和我们沙箱思路一致。");

/* P20 对我们的启示 */
vsSlide("TAKEAWAY · 启示",[{text:"对照 Harness：我们做对了什么，接下来做什么",options:{}}],
  ["✅ 已经做对的",["配置驱动：意图 / 智能体 / 技能全部声明式","能力分装：技能 = 文档 + 工具，卡片 = 模板 + schema","隔离与安全：沙箱工作区、权限白名单、单槽反馈对账"],GOOD],
  ["🧭 可以借鉴的",["插件化运行时：工具 / 技能从「配置注册」走向「热插拔」","可逆性设计：注册即登记撤销，组件可回滚可替换","统一模型服务接口：面向接口编程，模型随装随换"],ACCENT],
  [{text:"Agent 的竞争，正在从「模型效果」延伸到「",options:{color:T1}},{text:"运行时质量",options:{bold:true,color:ACCENT}},{text:"」。",options:{color:T1}}],
  "Part 04 · 启示",20,
  "收束观点：我们项目在企业场景里自建了一层实用主义 Harness，方向和 DeepSeek Harness 互相印证。后续演进可以考虑向插件化、可逆性靠拢。");

/* P21 谢谢 */
(()=>{ const s=baseSlide();
  s.addShape("ellipse",{x:10.35,y:0.32,w:2.5,h:2.5,fill:{color:ACCENT,transparency:92},line:{transparency:100}});
  s.addShape("ellipse",{x:0.42,y:4.72,w:2.3,h:2.3,fill:{color:ACCENT2,transparency:93},line:{transparency:100}});
  s.addText("谢谢",{x:MX,y:2.35,w:CW,h:1.35,fontFace:F,fontSize:64,bold:true,color:ACCENT,align:"center",valign:"middle",margin:0,charSpacing:6});
  s.addText("Model + Harness = Agent —— 愿我们既造好大脑，也造好手脚。",
    {x:1.8,y:4.0,w:SW-3.6,h:0.55,fontFace:F,fontSize:14,color:T2,align:"center",margin:0});
  s.addText("智能问答平台项目组      ·      Q & A      ·      2026 · 11",
    {x:1.8,y:5.05,w:SW-3.6,h:0.4,fontFace:F,fontSize:11,color:T3,align:"center",margin:0});
  notes(s,"结束语：留 5 分钟问答。感谢大家。");
})();

pres.writeFile({fileName:OUT}).then(()=>console.log("WROTE: "+OUT)).catch(e=>{console.error(e);process.exit(1);});
