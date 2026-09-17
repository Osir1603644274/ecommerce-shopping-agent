"""Offline anonymity and strict-score checks, before any review model call."""
from copy import deepcopy
import unittest
from .common import *
from .judging import make_packet, validate_judgment

class JudgeContractTests(unittest.TestCase):
    def packet(self,reviewer=1):
        f=read(HERE/'inputs/case-001.json')
        rows=[{'ordinal':i,'answer':f'这是匿名答案 {i}','arm':ARMS[i%3],'repeat':i//3+1} for i in range(9)]
        return make_packet(f,rows,reviewer)

    def value(self,packet):
        return {'packetId':packet['packetId'],'scores':[{'answerId':a['answerId'],'correctness':3,'constraints':3,
            'relevance':3,'usefulness':3,'seriousError':False,'insufficientEvidence':False,'issues':[],
            'rationale':'存在一处轻微遗漏；此处仅为离线契约测试。'} for a in packet['anonymousAnswers']]}

    def test_labels_private_evidence_complete(self):
        packet,mapping=self.packet();f=read(HERE/'inputs/case-001.json')
        self.assertEqual(len(mapping),9)
        self.assertEqual(packet['task']['completeUserHistory'],f['history'])
        self.assertEqual(packet['task']['providedEvidence']['products'],f['products'])
        self.assertEqual(packet['task']['providedReferenceState'],f['state'])
        self.assertNotIn('prompts',packet);self.assertNotIn('source',packet)
        self.assertEqual({a['answerId'] for a in packet['anonymousAnswers']},{a['answerId'] for a in mapping})
        for marker in ARMS+('cliWallMs','applicationInputTokens','expectedHard'):self.assertNotIn(marker,canonical(packet))

    def test_fresh_independent_packet_order(self):
        first,m1=self.packet(1);second,m2=self.packet(2)
        self.assertNotEqual(first['packetId'],second['packetId'])
        self.assertEqual(first['task'],second['task'])
        self.assertNotEqual([x['ordinal'] for x in m1],[x['ordinal'] for x in m2])
        self.assertEqual({a['text'] for a in first['anonymousAnswers']},{a['text'] for a in second['anonymousAnswers']})

    def test_score_contract_rejects_missing_duplicate_wrong_id_or_range(self):
        packet,_=self.packet();value=self.value(packet)
        self.assertEqual(validate_judgment(packet,canonical(value)),value)
        variants=[]
        v=deepcopy(value);v['scores'].pop();variants.append(v)
        v=deepcopy(value);v['scores'][0]['answerId']=v['scores'][1]['answerId'];variants.append(v)
        v=deepcopy(value);v['packetId']='wrong';variants.append(v)
        v=deepcopy(value);v['scores'][0]['correctness']=5;variants.append(v)
        v=deepcopy(value);v['scores'][0]['correctness']=True;variants.append(v)
        v=deepcopy(value);v['scores'][0]['rationale']=' ';variants.append(v)
        for v in variants:
            with self.assertRaises(Exception):validate_judgment(packet,canonical(v))

if __name__=='__main__':unittest.main(verbosity=2)
