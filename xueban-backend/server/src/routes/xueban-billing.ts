import { Router, type Response } from 'express';
import { XuebanChatError, xuebanChatCompany } from '../services/xueban-chat';
import { billingSummary, CUSTOM_PLAN, PLANS, upgradePlan, type PlanKey } from '../storage/billing';

const router = Router();

function fail(res: Response, error: unknown): void {
  const err = error instanceof XuebanChatError
    ? error
    : new XuebanChatError(500, 'BILLING_FAILED', error instanceof Error ? error.message : String(error));
  res.status(err.status).json({ error: { code: err.code, message: err.message } });
}

/** 套餐目录：页面卡片完全由后端数据驱动。 */
router.get('/plans', (_req, res) => {
  res.json({
    items: [
      { ...PLANS.basic, current: false },
      { ...PLANS.studio, current: false },
      { ...CUSTOM_PLAN, contact: true },
    ],
  });
});

router.get('/summary', (req, res) => {
  void (async () => {
    const company = await xuebanChatCompany(req.headers.authorization || '');
    res.json({ data: billingSummary(company.id) });
  })().catch(error => fail(res, error));
});

/** 模拟付款：不接真实支付，只校验套餐并立即生效。 */
router.post('/upgrade', (req, res) => {
  void (async () => {
    const company = await xuebanChatCompany(req.headers.authorization || '');
    const plan = String(req.body?.plan || '') as PlanKey;
    if (plan !== 'studio' && plan !== 'basic') {
      throw new XuebanChatError(422, 'INVALID_PLAN', '该套餐不支持在线升级，请联系开发者');
    }
    res.json({ data: upgradePlan(company.id, plan), meta: { simulated_payment: true } });
  })().catch(error => fail(res, error));
});

export default router;
