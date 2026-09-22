"""Aggregate every observed retry; replayed receipts are counted only once."""
def meter_requests(observations, *, supported_rejections=()):
    calls={};attempts={};tools={};effective=None
    for observed in observations:
        trace=observed.get('trace') or {}
        for attempt in trace.get('attempts',[]):
            identity=attempt.get('runId')
            if identity:attempts[identity]=attempt
            for tool in attempt.get('toolReceipts',[]):
                if tool.get('toolCallId'):tools[tool['toolCallId']]=tool
            receipt=attempt.get('modelReceipt') or {}
            if receipt.get('modelCallId'):calls[receipt['modelCallId']]=receipt
        if effective is None and (trace.get('status')=='COMPLETED' or trace.get('requestId') in supported_rejections):
            # Includes failed requests and elapsed recovery time before useful content.
            effective=observed.get('sinceFirstRequestMs')
    known=list(calls.values())
    result={'modelCalls':known,'modelMs':sum(c.get('durationMs',0) for c in known),
            'toolMs':sum(t.get('durationMs',0) for t in tools.values()),
            'elapsedMs':sum(o['elapsedMs'] for o in observations),
            'requestCount':len(observations),
            'meteringComplete':bool(attempts) and all(a.get('modelReceipt',{}).get('modelCallId') or a.get('modelReceipt',{}).get('status')=='REUSED_SAVED_PLAN' for a in attempts.values())}
    if effective is not None:result['firstContentMs']=effective
    return result
