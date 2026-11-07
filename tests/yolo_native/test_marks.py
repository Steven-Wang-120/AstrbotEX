import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'plugins/vision/yolo'))
from state import State


class MarkTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.path=self.temp.name+'/state.db'
        self.state=State(self.path)
        self.objects=[{'name':'cup','color':'red','box':[0,0,10,10],'feature':[1,0]},
                      {'name':'bottle','color':'blue','box':[50,0,60,10],'feature':[0,1]}]
        self.ids=self.state.associate(self.objects,now=100)
    def tearDown(self):
        self.state.close();self.temp.cleanup()
    def command(self,request,ids,marked):
        return {'request_id':request,'stream_id':self.state.stream_id,'ids':ids,'marked':marked}
    def test_multiple_marks_survive_frames_and_restart(self):
        self.state.mark(self.command('a',self.ids,True))
        for t in range(101,110):
            self.assertEqual(self.state.associate(self.objects,now=t),self.ids)
            self.assertTrue(all(self.state.snapshot()[1][x]==1 for x in self.ids))
        stream=self.state.stream_id
        self.state.close();self.state=State(self.path)
        self.assertEqual(stream,self.state.stream_id)
        self.assertTrue(all(self.state.snapshot()[1][x]==1 for x in self.ids))
        self.state.mark(self.command('b',[self.ids[0]],False))
        self.assertEqual([self.state.snapshot()[1][x] for x in self.ids],[0,1])
    def test_lost_mark_is_retained(self):
        self.state.mark(self.command('a',self.ids,True))
        self.state.associate([],now=1000000)
        self.assertEqual(set(self.state.snapshot()[2]),set(self.ids))
        self.assertTrue(all(not x['visible'] for x in self.state.snapshot()[2].values()))
    def test_idempotent_retry_does_not_reverse_newer_clear(self):
        command=self.command('a',self.ids,True)
        receipt=self.state.mark(command)
        self.state.mark(self.command('b',self.ids,False))
        self.assertEqual(receipt,self.state.mark(command))
        self.assertTrue(all(self.state.snapshot()[1][x]==0 for x in self.ids))
    def test_unknown_id_atomic_rejection(self):
        with self.assertRaises(ValueError):self.state.mark(self.command('a',[self.ids[0],'unknown'],True))
        self.assertEqual(self.state.snapshot()[1][self.ids[0]],0)
    def test_request_id_conflict_and_boolean_validation(self):
        self.state.mark(self.command('a',self.ids,True))
        with self.assertRaises(ValueError):self.state.mark(self.command('a',self.ids,False))
        with self.assertRaises(ValueError):self.state.mark(self.command('b',self.ids,'false'))
    def test_unknown_stream(self):
        command=self.command('a',self.ids,True);command['stream_id']='other'
        with self.assertRaises(ValueError):self.state.mark(command)
    def test_mark_not_inherited_by_different_class(self):
        self.state.mark(self.command('a',self.ids,True))
        objects=[{**self.objects[0],'name':'book'}]
        new=self.state.associate(objects,now=101)[0]
        self.assertNotIn(new,self.ids)
        self.assertEqual(self.state.snapshot()[1][new],0)
    def test_motion_keeps_identity(self):
        self.state.mark(self.command('a',self.ids,True))
        moved=[{**obj,'box':[v+2 for v in obj['box']]} for obj in self.objects]
        self.assertEqual(self.state.associate(moved,now=101),self.ids)
        self.assertTrue(all(self.state.snapshot()[1][x]==1 for x in self.ids))
    def test_ambiguous_lookalike_does_not_inherit_mark(self):
        second={**self.objects[0],'box':[100,0,110,10]}
        self.state.associate([self.objects[0],second],now=101)
        self.state.mark(self.command('a',[self.ids[0]],True))
        new=self.state.associate([{**self.objects[0],'box':[200,0,210,10]}],now=104)[0]
        self.assertEqual(self.state.snapshot()[1][new],0)
        self.assertEqual(self.state.snapshot()[1][self.ids[0]],1)


if __name__=='__main__':unittest.main()
