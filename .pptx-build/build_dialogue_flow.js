/* 对话全流程流程图 · 16:9 单页（Tokyo Night 风格）
 * 从用户发出消息 → 并行（意图改写/识别 × 沙箱装配）→ 编排映射 → Agent 执行 → 流式回答 */
const pptxgen = require("pptxgenjs");

const BG="1A1B26", SURFACE="24283B", SURFACE2="2F334D", BORDER="343A54",
  T1="C0CAF5", T2="A9B1D6", T3="565F89",
  ACCENT="7AA2F7", ACCENT2="BB9AF7", GOOD="9ECE6A", WARM="E0AF68", BAD="F7768E";
const F = "Microsoft YaHei";
const SW=13.333, SH=7.5, MX=0.5, CW=SW-2*MX;

const pres = new pptxgen();
pres.layout = "LAYOUT_WIDE";
pres.author = "智能问答平台项目组";
pres.title = "对话全流程";

function baseSlide(){ const s=pres.addSlide(); s.background={color:BG}; return s; }
function title(s, parts, sub){
  s.addText(parts,{x:MX,y:0.38,w:CW,h:0.6,fontFace:F,fontSize:26,bold:true,color:T1,align:"left",margin:0,valign:"middle"});
  if(sub) s.addText(sub,{x:MX,y:1.0,w:CW,h:0.34,fontFace:F,fontSize:11.5,color:T3,align:"left",margin:0,valign:"top"});
}
function mapper(S,X0,Y0){ return { S, X:(v)=>X0+v*S, Y:(v)=>Y0+v*S }; }
function cjkW(str,fs){ let w=0; for(const ch of str){ w += ch.charCodeAt(0)>255 ? 1 : 0.6; } return w*fs/72+0.08; }
function ctext(s,mx,cx,yBase,str,fs,color,opts={}){
  const w=opts.w||cjkW(str,fs), h=opts.h||fs*1.7/72;
  s.addText(str,Object.assign({x:mx.X(cx)-w/2,y:mx.Y(yBase)-h/2-fs*0.12/72,w,h,fontFace:F,fontSize:fs,color:color||T1,align:"center",valign:"middle",margin:0},opts.box||{}));
}
function ltext(s,mx,x,yBase,str,fs,color,wIn){
  const h=fs*1.7/72;
  const w=wIn? wIn*mx.S : Math.max(0.4, SW-MX-mx.X(x)); /* wIn 为 mapper 像素 */
  s.addText(str,{x:mx.X(x),y:mx.Y(yBase)-h/2-fs*0.12/72,w,h,fontFace:F,fontSize:fs,color:color||T1,align:"left",valign:"middle",margin:0});
}
function arrow(s,mx,x1,y1,x2,y2,o={}){
  const x=Math.min(x1,x2),y=Math.min(y1,y2),w=Math.abs(x2-x1)*mx.S,h=Math.abs(y2-y1)*mx.S;
  s.addShape("line",{x:mx.X(x),y:mx.Y(y),w,h,flipH:x2<x1,flipV:y2<y1,
    line:{color:o.color||ACCENT,width:o.width||1.5,dashType:o.dash?"dash":"solid",endArrowType:o.noArrow?undefined:"triangle"}});
}
function box(s,mx,x,y,w,h,o={}){
  s.addShape(o.shape||"roundRect",{x:mx.X(x),y:mx.Y(y),w:w*mx.S,h:h*mx.S,rectRadius:0.09,
    fill:o.fillNone?{type:"none"}:{color:o.fill||SURFACE,transparency:o.ft==null?0:o.ft},
    line:o.lineNone?{transparency:100}:{color:o.lineColor||BORDER,width:o.lw||0.9,dashType:o.dash?"dash":"solid"}});
}
function zone(s,mx,x,y,w,h,o={}){
  s.addShape("roundRect",{x:mx.X(x),y:mx.Y(y),w:w*mx.S,h:h*mx.S,rectRadius:0.12,
    fill:{color:o.fill||"1F2335",transparency:o.ft==null?80:o.ft},
    line:{color:o.lineColor||T3,width:o.lw||1,dashType:"dash"}});
}
function tblock(s,mx,x,y,w,rows,o={}){
  const flat=rows.flatMap(r=>Array.isArray(r)?r:[{text:r,options:{}}]); /* 拍平为 run 数组 */
  s.addText(flat,
    {x:mx.X(x),y:mx.Y(y),w:w*mx.S,h:(o.h||rows.length*20)*mx.S,fontFace:F,align:o.align||"left",valign:"top",margin:0,lineSpacingMultiple:1.32,fit:"shrink"});
}

/* ================================================================
 * 对话全流程 · 单页流程图
 * ================================================================ */
(()=>{ const s=baseSlide();
  title(s,
    [{text:"对话全流程：",options:{color:T1}},{text:"懒建沙箱 · 意图理解 · 编排执行 · 流式回答",options:{color:ACCENT}}],
    "登录仅融合配置、不建沙箱 —— 沙箱在首条对话时懒创建并注入技能 · 每轮对话均经过：改写 → 识别 → 编排 → 执行 → 流式输出");
  const mx=mapper(12.333/1760,0.5,1.45);

  /* ── 开始 ── */
  box(s,mx,280,15,1200,55,{fill:SURFACE2,lineColor:ACCENT,lw:1.1});
  ctext(s,mx,880,42.5,"用户发出一条消息 —— POST /chat 建立 SSE 流 · 鉴权解析 user_id / session_id",11,T1,{box:{bold:true}});
  arrow(s,mx,880,70,880,92);

  /* ── 并行区：意图理解（左） × 沙箱装配（右） ── */
  zone(s,mx,40,98,1680,240);
  ctext(s,mx,880,122,"并行执行 —— 意图理解（左）与沙箱装配（右）同时进行，压低首字延迟",9.5,ACCENT,{box:{bold:true}});
  /* 左列：① 改写 */
  box(s,mx,70,142,720,68,{fill:SURFACE,lineColor:ACCENT,lw:1.1});
  tblock(s,mx,86,144,688,[
    [{text:"① 意图改写",options:{fontSize:10,bold:true,color:T1,breakLine:true}}],
    [{text:"LLM 将模糊、缺上下文的原始问题补全为清晰、自含的指令",options:{fontSize:8.5,color:T2}}]
  ],{h:60});
  arrow(s,mx,430,210,430,234);
  /* 左列：② 识别 */
  box(s,mx,70,234,720,64,{fill:SURFACE,lineColor:ACCENT,lw:1.1});
  tblock(s,mx,86,236,688,[
    [{text:"② 意图识别",options:{fontSize:10,bold:true,color:T1,breakLine:true}}],
    [{text:"输出意图 ids + 意图间关联关系 relation（independent / fixed / dynamic）",options:{fontSize:8.5,color:T2}}]
  ],{h:58});
  ltext(s,mx,70,316,"意图候选集受配置白名单约束 —— 未授权智能体在路由阶段即不可达",8,T3,700);
  /* 右列：◇ 沙箱检查 + 两分支 */
  box(s,mx,930,142,740,68,{fill:SURFACE,lineColor:WARM,lw:1.2});
  tblock(s,mx,946,145,708,[
    [{text:"◇ 沙箱装配检查 —— 该用户是否已有存活沙箱？",options:{fontSize:10,bold:true,color:WARM,breakLine:true}}],
    [{text:"沙箱按 user_id 分配 · TTL 存活期内有效",options:{fontSize:8.5,color:T2}}]
  ],{h:60});
  arrow(s,mx,1125,210,1125,234,{color:WARM});
  arrow(s,mx,1510,210,1510,234,{color:WARM});
  box(s,mx,930,234,390,64,{fill:SURFACE,lineColor:ACCENT2,lw:1.1});
  tblock(s,mx,944,236,362,[
    [{text:"✗ 无 → 懒创建：新建沙箱",options:{fontSize:9.5,bold:true,color:ACCENT2,breakLine:true}}],
    [{text:"绑定技能文件注入 /workspace/skills/",options:{fontSize:8,color:T2}}]
  ],{h:58});
  box(s,mx,1350,234,320,64,{fill:SURFACE,lineColor:GOOD,lw:1.1});
  tblock(s,mx,1362,236,296,[
    [{text:"✓ 有 → 直接复用",options:{fontSize:9.5,bold:true,color:GOOD,breakLine:true}}],
    [{text:"存活期内技能不重注入",options:{fontSize:8,color:T2}}]
  ],{h:58});
  ltext(s,mx,930,316,"创建仅发生一次 · 存活期内所有对话轮直接复用",8,T3,740);
  arrow(s,mx,880,338,880,362);

  /* ── ③ 编排策略映射 ── */
  zone(s,mx,40,366,1680,184);
  ctext(s,mx,880,388,"③ 编排策略映射 —— 按意图关联关系选择执行策略（intent_config.yml · default_orchestration）",9.5,ACCENT,{box:{bold:true}});
  const orch=[
    {x:68,  lc:ACCENT, l1:"单一意图",       l2:"→ pipeline", l3:"直接顺序执行 · 无多意图汇总开销"},
    {x:481, lc:GOOD,   l1:"多意图 · 相互独立", l2:"→ parallel", l3:"并行执行 · 各分支 900s 超时"},
    {x:894, lc:ACCENT, l1:"多意图 · 顺序固定", l2:"→ pipeline", l3:"固定流水线 · 每步 900s 超时"},
    {x:1307,lc:ACCENT2,l1:"多意图 · 顺序动态", l2:"→ react",   l3:"动态决策下一步 · 最多 8 步"}
  ];
  orch.forEach(o=>{
    box(s,mx,o.x,412,385,112,{fill:SURFACE,lineColor:o.lc,lw:1.1});
    tblock(s,mx,o.x+14,420,357,[
      [{text:o.l1,options:{fontSize:10,bold:true,color:T1,breakLine:true}}],
      [{text:o.l2,options:{fontSize:11,bold:true,color:o.lc,breakLine:true}}],
      [{text:o.l3,options:{fontSize:8.5,color:T2}}]
    ],{h:96,align:"center"});
  });
  arrow(s,mx,880,550,880,574);

  /* ── ④ Agent 执行 + 交互分支 ── */
  box(s,mx,64,578,1060,96,{fill:"1F2335",lineColor:ACCENT2,lw:1.3});
  tblock(s,mx,82,590,1024,[
    [{text:"④ Agent 执行 · ReAct 循环",options:{fontSize:11,bold:true,color:T1,breakLine:true}}],
    [{text:"思考 → 调用工具 → 观察结果 → 再思考 …… 循环直至产出最终回答 · react_max_steps=8 · 单次工具调用 900s 超时",options:{fontSize:8.5,color:T2}}]
  ],{h:72});
  box(s,mx,1180,578,540,96,{fill:SURFACE,lineColor:WARM,lw:1.1,dash:true});
  tblock(s,mx,1196,590,508,[
    [{text:"交互分支 · 卡片确认",options:{fontSize:10,bold:true,color:WARM,breakLine:true}}],
    [{text:"工具挂起（单槽 · 300s 超时）→ 用户点击 → 同轮恢复执行",options:{fontSize:8.5,color:T2}}]
  ],{h:68});
  arrow(s,mx,1124,602,1180,602,{color:WARM,width:1.2});
  arrow(s,mx,1180,650,1124,650,{color:WARM,width:1.2});
  arrow(s,mx,880,674,880,700);

  /* ── ⑤ 流式回答 ── */
  box(s,mx,40,704,1680,86,{fill:SURFACE2,lineColor:GOOD,lw:1.2});
  tblock(s,mx,60,716,1640,[
    [{text:"⑤ 流式回答",options:{fontSize:11,bold:true,color:GOOD,breakLine:true}}],
    [{text:"SSE 文本流 + 组件流 → 前端逐字渲染 · 会话消息落库 · 沙箱存活期内复用 · TTL 过期自动淘汰",options:{fontSize:9,color:T2}}]
  ],{h:68,align:"center"});

  pres.writeFile({ fileName: "/workspace/security-designs/dialogue-flow.pptx" })
    .then(f=>console.log("WROTE:",f)).catch(e=>{ console.error(e); process.exit(1); });
})();
