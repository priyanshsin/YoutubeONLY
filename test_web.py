import tempfile, unittest, time, uuid, re
from pathlib import Path
from unittest.mock import patch, MagicMock
import app

class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.m=app.Manager(Path(self.temp.name));self.client=app.create_app(self.m).test_client()
    def test_schedule_and_mix(self):
        self.assertAlmostEqual(self.m.state['next_release']-time.time(),86400,delta=3)
        langs=[p['language'] for p in app.plans()]
        self.assertEqual(langs.count('Hindi'),1);self.assertEqual(langs.count('English'),1)
        self.assertEqual(len(langs),2)
        self.assertTrue(all('Motivational and energetic' in p['theme'] for p in app.plans()))
    def test_pages_security_and_gallery(self):
        self.assertEqual(self.client.get('/').status_code,200)
        self.assertEqual(self.client.get('/releases').status_code,200)
        self.assertEqual(self.client.get('/api/releases').json,[])
        self.assertEqual(self.client.post('/api/action',json={'action':'run'}).status_code,403)
        self.assertEqual(self.client.get('/',headers={'Host':'evil.example'}).status_code,403)
        self.assertEqual(self.client.get('/media/not-valid/1/video').status_code,404)
        self.m.preview=True
        token=re.search('name="csrf-token" content="([^"]+)"',self.client.get('/').text).group(1)
        self.assertEqual(self.client.post('/api/action',json={'action':'run'},headers={'X-CSRF-Token':token,'Origin':'http://localhost'}).status_code,409)
    def seed(self):
        batch={'id':str(uuid.uuid4()),'songs':app.plans(),'status':'generating','release_at':time.time()-2,'email':{'status':'pending'}}
        self.m.state['batches']=[batch['id']];self.m.state['current']=batch['id'];self.m.write_batch(batch);self.m.persist();return batch
    def fake_build(self,args):
        self.assertFalse(args.upload);self.assertEqual(args.seconds,240)
        args.out.mkdir(parents=True,exist_ok=True)
        (args.out/'song.mp4').write_bytes(b'video')
        app.pipeline.save_json(args.out/'metadata.json',{'title':'Test '+args.language})
        (args.out/'email-song.mp3').write_bytes(b'full-audio-fixture')
    def verify(self,folder,song):
        song.update(status='published',url='https://www.youtube.com/watch?v=test',published_at='2026-10-20T10:00:00Z')
    def test_two_public_songs_then_two_attachments_and_no_repeat(self):
        batch=self.seed();response=MagicMock(ok=True);response.json.return_value={'id':'mail-test'}
        with patch.object(app.pipeline,'build',side_effect=self.fake_build) as build, patch.object(app.pipeline,'upload') as upload, patch.object(self.m,'verify_publication',side_effect=self.verify),patch.object(self.m,'attachment',side_effect=lambda f:f/'email-song.mp3'),patch.object(app.requests,'post',return_value=response) as post:
            self.m.run_batch(batch['id'])
            self.assertEqual(build.call_count,2);self.assertEqual(upload.call_count,2)
            self.assertTrue(all(c.args[1]=='public' for c in upload.call_args_list))
            payload=post.call_args.kwargs['json'];self.assertEqual(len(payload['attachments']),2);self.assertIn('2 new songs',payload['subject'])
            self.assertIn('Published:',payload['text']);self.assertEqual(self.m.batch(batch['id'])['status'],'complete')
            self.assertEqual(len(self.client.get('/api/releases').json),2)
            self.m.run_batch(batch['id']);self.assertEqual(post.call_count,1);self.assertEqual(upload.call_count,2)
    def test_failed_generation_never_publishes(self):
        batch=self.seed()
        with patch.object(app.pipeline,'build',side_effect=RuntimeError('Provider down')),patch.object(app.pipeline,'upload') as upload:
            self.m.run_batch(batch['id']);upload.assert_not_called()
        self.assertFalse(self.m.state['enabled']);self.assertEqual(self.m.batch(batch['id'])['status'],'failed')
    def test_interrupted_batch_is_not_silently_repeated(self):
        batch=self.seed();restarted=app.Manager(self.m.root)
        self.assertFalse(restarted.state['enabled']);self.assertEqual(restarted.batch(batch['id'])['status'],'interrupted')

if __name__=='__main__':unittest.main()
