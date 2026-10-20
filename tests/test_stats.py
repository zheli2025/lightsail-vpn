import contextlib
import datetime
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch

ROOT=Path(__file__).resolve().parents[1]


def module(name,file):
    spec=importlib.util.spec_from_file_location(name,ROOT/'scripts'/file)
    m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


traffic=module('traffic','traffic-update.py')
usage=module('usage','domain-usage.py')
launcher=module('launcher','traffic-launcher.py')


class TestStats(unittest.TestCase):
    def test_manual_and_timer_use_same_instance_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'migrate.conf'
            path.write_text('[Service]\nEnvironment=LS_REGION=ap-northeast-1\nEnvironment=LS_INSTANCE_NAME=vpn-tokyo\nEnvironment=LS_PUBLIC_IP=1.1.1.1\n')
            config=launcher.settings(path)
            self.assertEqual(config['LS_REGION'],'ap-northeast-1')
            path.write_text(path.read_text().replace('LS_PUBLIC_IP=1.1.1.1','LS_PUBLIC_IP=8.8.8.8'))
            self.assertEqual(launcher.settings(path)['LS_PUBLIC_IP'],'8.8.8.8')
    def test_instance_name_priority(self):
        client=Mock(); client.get_instance.return_value={'instance':{'name':'vpn-new'}}
        with patch.object(traffic,'INSTANCE_NAME','vpn-new'):
            self.assertEqual(traffic.find_instance(client)['name'],'vpn-new')
        client.get_instances.assert_not_called()

    def test_subscription_headers(self):
        header=traffic.header_value(dict(upload=1,download=2,total=3,expire=4))
        self.assertIn('upload=1; download=2; total=3; expire=4',header)
        self.assertIn('Profile-Update-Interval 12',header)

    def test_month_bounds_and_quota(self):
        start,end=traffic.month_bounds(datetime.datetime(2026,10,20,tzinfo=datetime.timezone.utc))
        self.assertEqual((start.day,end.day),(1,31))
        self.assertEqual(traffic.total_bytes({'networking':{'monthlyTransfer':{'gbPerMonthAllocated':1000}}}),1000*1024**3)

    def test_domain_report_and_prune(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(usage,'DB_FILE',str(Path(tmp)/'usage.db')), patch.object(usage,'LOG_FILE',str(Path(tmp)/'usage.log')):
                conn=usage.connect_db()
                with conn:
                    usage.upsert_daily(conn,usage.today_str(),'google','google.com',100,200)
                    usage.upsert_daily(conn,'2000-01-01','其他','old.example',1,1)
                conn.close()
                out=io.StringIO()
                with contextlib.redirect_stdout(out): usage.cmd_report('today',True)
                report=json.loads(out.getvalue())
                self.assertEqual(report['total'],300)
                self.assertEqual(report['top'][0]['domain'],'google.com')
                with contextlib.redirect_stdout(io.StringIO()): usage.cmd_prune(180)
                self.assertEqual(usage.query_rows('all')[1:],(100,200))

    def test_category_and_tui_sort(self):
        self.assertEqual(usage.host_category('chatgpt.com')[0],'openai')
        rows=[('a','a.example',10,1),('b','b.example',1,100)]
        self.assertEqual(usage._tui_sorted(rows,'download')[0][1],'b.example')
