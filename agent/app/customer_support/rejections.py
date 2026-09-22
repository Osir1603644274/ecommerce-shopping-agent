"""Explain authoritative business rejections without inventing available quantities."""

EXPLANATIONS = {
    'QUANTITY_EXCEEDS_AVAILABLE': '申请数量超过当前可办理数量。请减少数量后重新预览。',
    'PAYMENT_NOT_PAID': '当前支付记录不满足售后申请条件，请先核对本单支付状态。',
    'ORDER_OPERATION_IN_PROGRESS': '本单还有处理中操作，请先刷新订单进度，确认结果后再申请。',
    'APPLICATION_WINDOW_EXPIRED': '本单已超过签收后七天的申请期限。如有争议，可提交关联本单的核实工单。',
    'EXCHANGE_MUST_MATCH_PURCHASE': '换货仅支持原商品的同款同规格，请核对商品和规格后重新预览。',
    'NOT_RECEIVED': '本单尚不满足退货或换货的收货条件，请先核对物流签收状态。',
    'RECEIPT_NOT_CONFIRMED': '系统尚未确认签收，请先核对物流记录；记录不一致时可提交核实工单。',
    'INVALID_QUANTITY_EVIDENCE': '可办理数量的记录需要核实，请提交关联本单的核实工单。',
    'INVALID_DISPATCH_EVIDENCE': '出库记录需要核实，请提交关联本单的核实工单。',
    'DISPATCH_ALREADY_CLAIMED': '出库操作已经开始，当前无法按未发货路径办理，请先核对物流进度。',
    'INVALID_RECEIPT_TIME': '签收时间记录需要核实，请提交关联本单的核实工单。',
    'MISSING_PURCHASE_SPECIFICATION': '原订单缺少可核验的商品规格，请提交核实工单，不自动选择替代规格。',
}


def rejection_answer(detail):
    explanation = EXPLANATIONS.get(detail) if isinstance(detail, str) else None
    if explanation is None:
        # Unknown internal errors are not useful instructions or verified facts.
        explanation = '当前业务条件不满足。请刷新本单状态，或提交信息核实工单。'
    return '服务端尚未受理这项请求：' + explanation
