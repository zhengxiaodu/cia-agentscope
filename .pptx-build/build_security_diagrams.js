/* 安全设计图表 · 3 张 16:9 高清图（Tokyo Night 风格）
 * 图1 纵深防御总览 | 图2 沙箱隔离模型 | 图3 网络出口与文件通道管控 */
const pptxgen = require("pptxgenjs");

const BG="1A1B26", SURFACE="24283B", SURFACE2="2F334D", BORDER="343A54",
  T1="C0CAF5", T2="A9B1D6", T3="565F89",
  ACCENT="7AA2F7", ACCENT2="BB9AF7", GOOD="9ECE6A", WARM="E0AF68", BAD="F7768E";
const F = "Microsoft YaHei";
const SW=13.333, SH=7.5, MX=0.5, CW=SW-2*MX;

const pres = new pptxgen();
pres.layout = "LAYOUT_WIDE";
pres.author = "智能问答平台项目组";
pres.title = "安全设计图表";

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
function rtext(s,mx,xRight,yBase,str,fs,color){
  const ww=cjkW(str,fs), h=fs*1.7/72;
  s.addText(str,{x:mx.X(xRight)-ww,y:mx.Y(yBase)-h/2-fs*0.12/72,w:ww,h,fontFace:F,fontSize:fs,color:color||T1,align:"right",valign:"middle",margin:0});
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
function tblock(s,mx,x,y,w,rows,o={}){
  const flat=rows.flatMap(r=>Array.isArray(r)?r:[{text:r,options:{}}]); /* 拍平为 pptxgenjs 认识的 run 数组 */
  s.addText(flat,
    {x:mx.X(x),y:mx.Y(y),w:w*mx.S,h:(o.h||rows.length*20)*mx.S,fontFace:F,align:o.align||"left",valign:"top",margin:0,lineSpacingMultiple:1.32,fit:"shrink"});
}

/* ================================================================
 * 图 1 · 纵深防御总览
 * ================================================================ */
(()=>{ const s=baseSlide();
  title(s,[{text:"安全设计总览：",options:{color:T1}},{text:"四层纵深防御",options:{color:ACCENT}}],
    "请求从左到右流动，每一层只放行「被明确授权」的行为 —— 认证 · 最小权限 · 隔离 · 通道管控");
  const mx=mapper(12.333/1760,0.5,1.52);
  const layers=[
    { x:20,  w:390, name:"① 接入层", tag:"谁是用户 · 能传什么" },
    { x:445, w:390, name:"② 编排层", tag:"谁能干什么 · 用哪些工具" },
    { x:870, w:390, name:"③ 执行层", tag:"在哪干活 · 碰得到什么" },
    { x:1295,w:445,name:"④ 数据层", tag:"内容存在哪 · 怎么给模型" }];
  const L0=i=>layers[i].x;
  layers.forEach(L=>{
    box(s,mx,L.x,30,L.w,600,{fill:SURFACE,ft:62,lineColor:ACCENT,lw:1.1});
    ctext(s,mx,L.x+L.w/2,64,L.name,13,T1,{box:{bold:true}});
    ctext(s,mx,L.x+L.w/2,92,L.tag,8.5,T3);
  });
  /* 卡片行：4 行，y=120,200,280,360，盒高 86 */
  const rows1=[
    ["登录认证","JWT 令牌校验 · 会话绑定 user_id"],
    ["权限融合","登录时合并 mng 配置与用户权限 → Redis 用户级缓存 · 能见哪些智能体由权限决定"],
    ["上传三道闸","大小上限校验（413）· MIME 白名单（415）· 记录绑定 session/user"],
    ["上传不落盘","原始文件不写入沙箱与宿主机 · 删除/查询均校验文件归属（他人文件 404）"]];
  const rows2=[
    ["意图路由按权限","未授权智能体不在候选集，路由阶段就不可达"],
    ["工具最小权限","按绑定技能子集组装工具箱：没绑 = 没有，注册表硬约束"],
    ["工具工厂闭包","用户/会话上下文随工具闭包注入，拿不到越界上下文"],
    ["反馈槽位控制","单会话单槽 · feedbackId 对账 · 300s 超时 · 流中断联动取消"]];
  const rows4=[
    ["上传内容入库","解析内容存 DB，不落沙箱/宿主机文件系统"],
    ["提示词注入","问答时解析内容作为上下文注入提示词 · 沙箱内工具摸不到文件物理路径"],
    ["产物留在工作区","文件产物只写本会话目录，越界路径被拒绝"],
    ["会话结束","TTL 到期自动淘汰工作区，不留跨会话残留"]];
  [[rows1,0,322],[rows2,1,322],[rows4,3,380]].forEach(([rows,li,tw])=>{
    rows.forEach((r,i)=>{ const y=120+i*130;
      box(s,mx,L0(li)+20,y-14,li===3?400:350,122,{fill:SURFACE2,r:8});
      tblock(s,mx,L0(li)+34,y-2,tw,[[{text:r[0],options:{fontSize:10.5,bold:true,color:T1,breakLine:true}},{text:r[1],options:{fontSize:8,color:T2}}]],{h:104});
    });
  });
  /* ③ 执行层（高亮沙箱） */
  box(s,mx,L0(2)+20,106,350,340,{fill:"1F2335",lineColor:ACCENT,lw:1.3});
  ctext(s,mx,L0(2)+195,140,"OpenSandbox 沙箱",12,T1,{box:{bold:true}});
  [["· 按用户分配沙箱 · 用户间硬隔离",200],
   ["· 按 session 隔离目录 · 会话互不可见",240],
   ["· 六类文件命令全走适配器进沙箱",280],
   ["· 技能只注入 /workspace/skills/",320],
   ["· TTL 过期销毁 · 崩溃重建 · 定时清扫",360]].forEach(r=>{
    ltext(s,mx,L0(2)+36,r[1],r[0],8.5,T2,300);
  });
  /* 层间箭头 */
  arrow(s,mx,410,330,443,330); arrow(s,mx,835,330,868,330); arrow(s,mx,1260,330,1293,330);
  /* 底部设计原则条 */
  box(s,mx,20,650,1720,72,{fill:SURFACE2,r:10});
  ctext(s,mx,880,686,"设计原则：模型输出一律视为不可信 —— 代码跑在沙箱里 · 外联只留一条受控通道 · 权限逐级收窄 · 文件与执行环境物理分离",9.5,T2);
})();

/* ================================================================
 * 图 2 · 沙箱隔离模型
 * ================================================================ */
(()=>{ const s=baseSlide();
  title(s,[{text:"沙箱隔离模型：",options:{color:T1}},{text:"用户 × 会话 × 技能 三重边界",options:{color:ACCENT}}],
    "模型生成的任何内容都先当不可信代码对待 —— 跑在谁也看不见谁的隔离边界内");
  const mx=mapper(12.333/1760,0.5,1.52);
  /* 用户 A 沙箱 */
  ctext(s,mx,440,60,"用户 A",11,T1,{box:{bold:true}});
  box(s,mx,80,80,720,470,{fill:"1F2335",lineColor:ACCENT,lw:1.3});
  ctext(s,mx,440,112,"用户 A 专属沙箱（wid 按 user_id 分配）",10,ACCENT);
  /* 会话工作区 1/2 */
  box(s,mx,110,140,300,260,{fill:SURFACE,r:10,lineColor:GOOD,lw:1.1});
  ctext(s,mx,260,168,"会话 1 工作区",10,GOOD,{box:{bold:true}});
  tblock(s,mx,126,190,268,[
    [{text:"/data/workspaces/{session_1}",options:{fontSize:8,color:T3,breakLine:true}}],
    [{text:"· 问题文件 · 图表脚本 · 产物",options:{fontSize:8,color:T2,breakLine:true}}],
    [{text:"· 产物只归本会话所有",options:{fontSize:8,color:T2}}]],{h:170});
  box(s,mx,470,140,300,260,{fill:SURFACE,r:10,lineColor:GOOD,lw:1.1});
  ctext(s,mx,620,168,"会话 2 工作区",10,GOOD,{box:{bold:true}});
  tblock(s,mx,486,190,268,[
    [{text:"/data/workspaces/{session_2}",options:{fontSize:8,color:T3,breakLine:true}}],
    [{text:"· 同一用户，多会话隔离",options:{fontSize:8,color:T2,breakLine:true}}],
    [{text:"· 互不干扰，可并行",options:{fontSize:8,color:T2}}]],{h:170});
  /* 中缝虚线 + × */
  s.addShape("line",{x:mx.X(440),y:mx.Y(150),w:0,h:240*mx.S,line:{color:T3,width:1,dashType:"dash"}});
  ctext(s,mx,440,262,"×",11,T3);
  /* 技能注入条（沙箱底部） */
  box(s,mx,110,430,660,80,{fill:"2A2F4A",r:8,lineColor:ACCENT2,lw:1,dash:true});
  tblock(s,mx,128,444,630,[
    [{text:"技能注入区 /workspace/skills/{技能名}/",options:{fontSize:9.5,bold:true,color:ACCENT2,breakLine:true}}],
    [{text:"首次对话时懒注入 · 只注入该智能体绑定技能的文件 · 沙箱存活期内复用",options:{fontSize:8,color:T2}}]],{h:56});
  /* 用户 B 沙箱 */
  ctext(s,mx,1290,60,"用户 B",11,T1,{box:{bold:true}});
  box(s,mx,930,80,720,470,{fill:"1F2335",lineColor:ACCENT,lw:1.3});
  ctext(s,mx,1290,112,"用户 B 专属沙箱",10,ACCENT);
  box(s,mx,960,140,660,150,{fill:SURFACE,r:10});
  tblock(s,mx,978,158,624,[
    [{text:"同一套机制：会话目录隔离 + 技能注入 + TTL 生命周期",options:{fontSize:9.5,bold:true,color:T1,breakLine:true}}],
    [{text:"用户 B 沙箱与用户 A 沙箱之间：网络、文件系统、进程完全隔离",options:{fontSize:8.5,color:T2}}]],{h:116});
  box(s,mx,960,310,660,180,{fill:SURFACE,r:10});
  tblock(s,mx,978,328,624,[
    [{text:"生命周期防线（TTL + sweeper）",options:{fontSize:10,bold:true,color:T1,breakLine:true}}],
    [{text:"· last_access 超过 TTL → 销毁旧沙箱，下次对话重建",options:{fontSize:8.5,color:T2,breakLine:true}}],
    [{text:"· 会话目录不可用 → 判定崩溃，自动拉新",options:{fontSize:8.5,color:T2,breakLine:true}}],
    [{text:"· 后台 sweeper 定时清扫过期工作区，不留残留",options:{fontSize:8.5,color:T2}}]],{h:150});
  /* 中间隔离墙 */
  box(s,mx,830,60,80,530,{fill:SURFACE2,r:10});
  ctext(s,mx,870,290,"≠",16,T1);
  ctext(s,mx,870,350,"用户间",8.5,T2);
  ctext(s,mx,870,374,"硬隔离",8.5,T2);
  /* 底部：执行通道 */
  box(s,mx,20,650,1720,72,{fill:SURFACE2,r:10});
  ctext(s,mx,880,676,"唯一执行通道",10,T1,{box:{bold:true}});
  ltext(s,mx,300,706,"智能体 → OpenSandboxAdapter → 沙箱内 Bash / Read / Write / Edit / Glob / Grep —— 不存在第二条进入宿主机的路径",8.5,T2,1400);
})();

/* ================================================================
 * 图 3 · 网络出口与文件通道管控
 * ================================================================ */
(()=>{ const s=baseSlide();
  title(s,[{text:"通道管控：",options:{color:T1}},{text:"外联一条路 · 文件不进沙箱",options:{color:ACCENT}}],
    "联网路径唯一（只能走技能工具）· 上传文件解析后经提示词注入，与执行环境物理分离");
  const mx=mapper(12.333/1760,0.5,1.52);
  /* 左：禁止区 */
  box(s,mx,20,30,540,300,{fill:"2A1F2E",lineColor:BAD,lw:1.2});
  ctext(s,mx,290,58,"直接外联 · 一律封死",12,BAD,{box:{bold:true}});
  [["✗ curl / wget / requests 等指令",130],
   ["✗ 自行访问外部接口（system prompt 明令禁止）",175],
   ["✗ 沙箱内无免费网络出口",220],
   ["✗ 未绑定技能的工具，调用入口不存在",265]].forEach(r=>ltext(s,mx,48,r[1],r[0],9,T2,480));
  /* 中：唯一出口 */
  box(s,mx,620,30,520,300,{fill:"1F2335",lineColor:GOOD,lw:1.3});
  ctext(s,mx,880,58,"唯一网络出口",12,GOOD,{box:{bold:true}});
  box(s,mx,650,100,460,80,{fill:SURFACE,r:10,lineColor:GOOD,lw:1});
  tblock(s,mx,672,116,420,[
    [{text:"联网搜索工具（web search）",options:{fontSize:11,bold:true,color:T1,breakLine:true}}],
    [{text:"后端进程内执行 · 代理出网 · 结果回注对话",options:{fontSize:8.5,color:T2}}]],{h:56});
  ctext(s,mx,880,222,"双重闸门",10,T1,{box:{bold:true}});
  ctext(s,mx,880,262,"① 配置开关：search_enabled=false 时工具根本不注入",9,T2);
  ctext(s,mx,880,296,"② 技能绑定：未绑定搜索技能的智能体拿不到这个工具",9,T2);
  /* 右：文件通道 */
  box(s,mx,1200,30,540,300,{fill:"1F2335",lineColor:WARM,lw:1.3});
  ctext(s,mx,1470,58,"文件通道 · 与沙箱分离",12,WARM,{box:{bold:true}});
  [["① 上传：大小上限 + MIME 白名单两道闸",135],
   ["② 解析：MinerU / 转写 / 直读，内容写 DB",175],
   ["③ 不落盘：原始文件不进沙箱与宿主机",215],
   ["④ 注入：问答时解析内容经提示词给 agent",255],
   ["⑤ 归属：删除/查询均校验 user_id，他人 404",295]].forEach(r=>ltext(s,mx,1228,r[1],r[0],9,T2,480));
  /* 连接箭头 */
  arrow(s,mx,562,170,616,170,{color:BAD});
  rtext(s,mx,548,148,"被禁止",8.5,BAD);
  arrow(s,mx,562,240,616,240,{color:GOOD});
  rtext(s,mx,548,262,"仅此一条",8.5,GOOD);
  /* 底部：工具箱互斥对比 */
  box(s,mx,20,360,1720,290,{fill:SURFACE,ft:62,lineColor:ACCENT,lw:1});
  ctext(s,mx,880,390,"工具最小权限：每个智能体的工具箱 = 自己绑定的技能子集（注册表硬约束，互不相通）",12,T1,{box:{bold:true}});
  box(s,mx,60,415,770,225,{fill:"1F2335",r:10});
  ctext(s,mx,445,452,"联网搜索智能体",11,T1,{box:{bold:true}});
  [["✓ web_search（唯一出口）",505,GOOD],["✓ 知识库检索",545,GOOD],["✗ 无文件系统写入工具",590,BAD],["✗ 无卡片/导出等未绑定工具",625,BAD]].forEach(r=>ltext(s,mx,100,r[1],r[0],9,r[2],690));
  box(s,mx,910,415,770,225,{fill:"1F2335",r:10});
  ctext(s,mx,1295,452,"数据可视化智能体",11,T1,{box:{bold:true}});
  [["✓ 沙箱 Bash/Write（画图表）",505,GOOD],["✓ 渲染卡片 + 反馈确认",545,GOOD],["✗ 无 web_search 工具",590,BAD],["✗ 无知识库工具（未绑定）",625,BAD]].forEach(r=>ltext(s,mx,950,r[1],r[0],9,r[2],690));
  ctext(s,mx,870,560,"≠",13,T3);
  /* 底部结论条 */
  box(s,mx,20,670,1720,54,{fill:SURFACE2,r:10});
  ctext(s,mx,880,697,"搜索智能体拿不到文件工具 → 文件内容不会被外发 · 画图智能体拿不到搜索出口 → 处理文件时不会误触外联",9,T2);
})();

pres.writeFile({fileName:"/workspace/security-designs/security-designs.pptx"})
  .then(()=>console.log("WROTE security-designs.pptx")).catch(e=>{console.error(e);process.exit(1);});
