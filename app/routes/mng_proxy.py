from fastapi import APIRouter, HTTPException
import httpx

from app.config import MNG_URL

router = APIRouter()


@router.get("/api/ui/presentation/cards")
async def proxy_card_configs():
    return {
              "success": True,
              "message": None,
              "data": [
                {
                  "id": 1,
                  "appId": "default",
                  "cardType": "metric",
                  "cardName": "指标卡",
                  "cardDesc": "展示股票关键指标",
                  "schemaFields": """{"stock_name":"贵州茅台","stock_code":"600519.SH","current_price":1856.50,"change_pct":1.20,"pe_ratio":32.5,"market_cap":"2.33万亿","turnover_rate":8.2,"support_level":1845,"resistance_level":1920,"rating":"买入评级"}""",
                  "triggerRule": "true",
                  "fallbackType": "text",
                  "renderTemplate": """var sn=schema.stock_name||"贵州茅台";
                    var cp=schema.current_price||1856.50;
                    var chg=schema.change_pct||1.20;
                    var pe=schema.pe_ratio||32.5;
                    var mc=schema.market_cap||"2.33万亿";
                    var tr=schema.turnover_rate||8.2;
                    var sl=schema.support_level||1845;
                    var rl=schema.resistance_level||1920;
                    var rt=schema.rating||"买入评级";
                    return ''<div class="preview-card-demo"><div class="preview-card-header" style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px"><div style="display:flex;align-items:center;gap:8px;font-weight:600"><svg class="icon icon-sm" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/></svg> 核心指标 - ''+sn+''</div><span style="background:rgba(16,185,129,0.2);color:var(--accent-success);padding:2px 8px;border-radius:4px;font-size:11px">''+rt+''</span></div><div style="padding:10px 0"><div style="display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:10px"><div style="text-align:center;padding:12px;background:var(--bg-elevated);border-radius:8px;border:1px solid rgba(16,185,129,0.2)"><div style="font-size:18px;font-weight:700;color:var(--accent-success);font-family:var(--font-display)">''+cp.toFixed(2)+''</div><div style="font-size:10px;color:var(--text-muted)">当前价</div><div style="font-size:9px;color:var(--accent-success)">+''+chg.toFixed(2)+''%</div></div><div style="text-align:center;padding:12px;background:var(--bg-elevated);border-radius:8px;border:1px solid rgba(96,165,250,0.2)"><div style="font-size:18px;font-weight:700;color:var(--accent-primary);font-family:var(--font-display)">''+pe+''x</div><div style="font-size:10px;color:var(--text-muted)">市盈率(PE)</div><div style="font-size:9px;color:var(--text-muted)">行业均值28x</div></div><div style="text-align:center;padding:12px;background:var(--bg-elevated);border-radius:8px;border:1px solid rgba(139,92,246,0.2)"><div style="font-size:18px;font-weight:700;color:var(--accent-tertiary);font-family:var(--font-display)">''+mc+''</div><div style="font-size:10px;color:var(--text-muted)">总市值</div><div style="font-size:9px;color:var(--text-muted)">A股第1</div></div></div><div style="display:grid;grid-template-columns:repeat(3,1fr);gap:10px"><div style="text-align:center;padding:10px;background:var(--bg-elevated);border-radius:8px"><div style="font-size:14px;font-weight:600;color:var(--text-primary)">''+tr+''%</div><div style="font-size:9px;color:var(--text-muted)">换手率</div></div><div style="text-align:center;padding:10px;background:var(--bg-elevated);border-radius:8px"><div style="font-size:14px;font-weight:600;color:var(--text-primary)">''+sl+''</div><div style="font-size:9px;color:var(--text-muted)">支撑位</div></div><div style="text-align:center;padding:10px;background:var(--bg-elevated);border-radius:8px"><div style="font-size:14px;font-weight:600;color:var(--text-primary)">''+rl+''</div><div style="font-size:9px;color:var(--text-muted)">压力位</div></div></div></div></div>'';""",
                  "isEnabled": 1,
                  "sortOrder": 1,
                  "createTime": "2024-01-01T10:00:00",
                  "updateTime": "2024-01-01T10:00:00"
                }
              ]
            }
    if not MNG_URL:
        raise HTTPException(status_code=500, detail="MNG_URL not configured")
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(f"{MNG_URL}/ui/presentation/cards")
        return resp.json()


@router.get("/api/ui/presentation/custom-components")
async def proxy_custom_component_configs():
    return {"success": True, "message": None, "data": []}
    if not MNG_URL:
        raise HTTPException(status_code=500, detail="MNG_URL not configured")
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(f"{MNG_URL}/ui/presentation/custom-components")
        return resp.json()