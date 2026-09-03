local stockKey = KEYS[1]
local buyersKey = KEYS[2]
local streamKey = KEYS[3]
local readyKey = KEYS[4]

if redis.call('EXISTS', readyKey) == 0 or redis.call('EXISTS', stockKey) == 0 then
    return 3
end

local stock = tonumber(redis.call('GET', stockKey))
if stock == nil or stock <= 0 then
    return 1
end

if redis.call('SISMEMBER', buyersKey, ARGV[2]) == 1 then
    return 2
end

redis.call('DECR', stockKey)
redis.call('SADD', buyersKey, ARGV[2])
redis.call(
    'XADD', streamKey, '*',
    'orderId', ARGV[1],
    'userId', ARGV[2],
    'campaignId', ARGV[3],
    'amountMinor', ARGV[4]
)
return 0
