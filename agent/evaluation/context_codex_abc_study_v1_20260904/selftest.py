"""Offline gates only; never samples a model or mutates a business state."""
import asyncio
from collections import Counter
import unittest
import sys
from .common import *
from .dataset import build_context, derive_hard, requirements
from .runner import validate

class FrozenStudyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = verify()
        cls.cases = [read(HERE / 'inputs' / f'case-{i:03d}.json') for i in range(1,49)]
        cls.schedule = read(HERE / 'inputs/schedule.json')

    def test_schedule(self):
        self.assertEqual(len(self.schedule),444)
        self.assertEqual([r['ordinal'] for r in self.schedule], list(range(1,445)))
        study = [r for r in self.schedule if r['kind']=='study']
        self.assertEqual(len(study),432)
        self.assertEqual(set(Counter((r['caseId'],r['arm']) for r in study).values()),{3})
        self.assertEqual(len(set((r['caseId'],r['arm'],r['repeat']) for r in study)),432)
        controls = [r for r in self.schedule if r['kind']=='identical_input_control']
        self.assertEqual(len(controls),12)
        self.assertEqual({(r['caseId'],r['arm']) for r in controls},{('case-001','A_RAW')})
        self.assertEqual(Counter(r['repeat'] for r in controls),{1:4,2:4,3:4})

    def test_schema_and_reference_state(self):
        for f in self.cases:
            self.assertEqual(f['referenceHardBefore'],derive_hard(f['source']['texts'] if f['stage']=='final' else f['source']['texts'][:-1]))
            if f['stage']=='state':
                self.assertEqual(derive_hard(f['source']['texts']),f['oracle']['expectedHard'])
                self.assertEqual(f['schema'],read(HERE / 'inputs' / (f['id']+'.schema.json')))
                llm,_,_,_,_,State,_,_,_=p.load_app()
                expected=f['oracle']['expectedHard']
                native = {'status':'ready','domainStatePatch':{'shoppingGuide':{'upsertRequirements':requirements(expected),
                    'removeRequirementKeys':[k for k in f['referenceHardBefore'] if k not in expected]}}}
                payload,_=llm._build_validated_task_state_payload(State.model_validate(f['state']),native,message=f['query'],require_status=True)
                self.assertIsInstance(payload,dict)
        self.assertEqual(requirements({'os':'android'})[0]['unit'],'enum')
        self.assertEqual(derive_hard(['预算2000元，存储至少256GB，只看ios','取消预算，取消存储，苹果或者安卓都可以']),{})

    def test_isolation_and_measurement(self):
        for f in self.cases:
            for arm in ARMS:
                text=f['prompts'][arm]
                self.assertEqual(tokens(text),f['appInputTokens'][arm])
                self.assertTrue(text.endswith('当前用户请求：'+f['query']))
                for marker in ('"oracle"','"expectedHard"','"sourceRowHash"','"absentKeys"'):
                    self.assertNotIn(marker,text)
            self.assertTrue(all(h['content'] in f['prompts']['A_RAW'] for h in f['history']))
            self.assertEqual(f['raw']['validatedResults'],f['products'])
            self.assertEqual(f['raw']['taskState'],f['state'])
            self.assertEqual(f['sameApplicationInputBC'],f['prompts']['B_PACK']==f['prompts']['C_PACK_COMPILER'])

    def test_all_contexts_repeat_exactly(self):
        for f in self.cases:
            for arm in ARMS:
                prompt,context,_=asyncio.run(build_context(f,arm))
                self.assertEqual(prompt,f['prompts'][arm],f['id']+arm)
                self.assertEqual(context,f['contexts'][arm],f['id']+arm)

    def test_boundary_configuration(self):
        values=p.effective_overrides(HERE)
        self.assertEqual(values['sandbox_mode'],'read-only')
        self.assertEqual(values['forced_login_method'],'chatgpt')
        for feature in p.DISABLED: self.assertFalse(values['features.'+feature])
        self.assertFalse(any(k in p.clean_env() for k in ('OPENAI_API_KEY','DEEPSEEK_API_KEY','CODEX_API_KEY')))
        self.assertEqual(self.manifest['compilerBudgetEstimatedTokens'],20000)

if __name__=='__main__':
    suite=unittest.defaultTestLoader.loadTestsFromTestCase(FrozenStudyTests)
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    if result.wasSuccessful():
        write_new(HERE / 'offline_gate.json', {'at':now(),'passed':result.testsRun,
            'selftestHash':file_sha(__file__),'manifestHash':file_sha(HERE / 'inputs/manifest.json'), 'sampledModelCalls':0})
    sys.exit(0 if result.wasSuccessful() else 1)
