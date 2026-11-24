import csv
import io

from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APITestCase

from datetime import datetime
from zoneinfo import ZoneInfo
NPT = ZoneInfo("Asia/Kathmandu")

from apps.course.models import Course
from apps.lead.models import Lead

User = get_user_model()


class PrimaryCourseNestedTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            email='admin@test.com', name='Admin', password='pass', is_staff=True,
        )
        self.client.force_authenticate(self.admin)
        self.course = Course.objects.create(name='Python Advanced', full_amount=30000)
        self.lead = Lead.objects.create(
            full_name='Test Lead', phone='9800000055', primary_course=self.course,
        )

    def test_detail_returns_nested_primary_course(self):
        response = self.client.get(f'/api/leads/{self.lead.id}/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        pc = response.data['primary_course']
        self.assertEqual(pc, {'id': self.course.id, 'name': 'Python Advanced', 'price': '30000.00'})
        self.assertNotIn('primary_course_name', response.data)

    def test_null_primary_course(self):
        lead = Lead.objects.create(full_name='No Course', phone='9800000056')
        response = self.client.get(f'/api/leads/{lead.id}/')
        self.assertIsNone(response.data['primary_course'])

    def test_patch_accepts_nested_object(self):
        other = Course.objects.create(name='Flutter', full_amount=25000)
        response = self.client.patch(
            f'/api/leads/{self.lead.id}/',
            {'primary_course': {'id': other.id}}, format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['primary_course']['id'], other.id)

    def test_patch_still_accepts_bare_int(self):
        bare = Course.objects.create(name='DevOps', full_amount=None)
        response = self.client.patch(
            f'/api/leads/{self.lead.id}/',
            {'primary_course': bare.id}, format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['primary_course'], {
            'id': bare.id, 'name': 'DevOps', 'price': None,
        })

    def test_patch_invalid_course_rejected(self):
        response = self.client.patch(
            f'/api/leads/{self.lead.id}/',
            {'primary_course': 99999}, format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

class LeadSourceDeletionTests(APITestCase):
    def setUp(self):
        from apps.lead.models import LeadSource
        self.admin = User.objects.create_user(
            email='admin@test.com', name='Admin', password='pass', is_staff=True,
        )
        self.client.force_authenticate(self.admin)
        self.protected = LeadSource.objects.create(
            name='Protected Source', is_protected=True,
        )
        self.used = LeadSource.objects.create(name='Used Source')
        Lead.objects.create(full_name='Someone', phone='1', source=self.used)
        self.free = LeadSource.objects.create(name='Free Source')

    def test_protected_source_cannot_be_deleted(self):
        response = self.client.delete(f'/api/lead-sources/{self.protected.id}/')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('protected', response.data['detail'])
        self.assertTrue(
            __import__('apps.lead.models', fromlist=['LeadSource'])
            .LeadSource.objects.filter(pk=self.protected.id).exists()
        )

    def test_used_source_cannot_be_deleted(self):
        response = self.client.delete(f'/api/lead-sources/{self.used.id}/')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('used in leads', response.data['detail'])

    def test_unused_unprotected_source_deletes(self):
        response = self.client.delete(f'/api/lead-sources/{self.free.id}/')
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    def test_is_protected_toggle_via_patch(self):
        response = self.client.patch(
            f'/api/lead-sources/{self.free.id}/',
            {'is_protected': True}, format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data['is_protected'])
        # now deletion must be blocked
        blocked = self.client.delete(f'/api/lead-sources/{self.free.id}/')
        self.assertEqual(blocked.status_code, status.HTTP_400_BAD_REQUEST)

    def test_model_level_delete_raises_protected_error(self):
        from django.db import transaction
        from django.db.models import ProtectedError
        with self.assertRaises(ProtectedError):
            with transaction.atomic():
                self.protected.delete()
        with self.assertRaises(ProtectedError):
            with transaction.atomic():
                self.used.delete()


class GlobalCallListTests(APITestCase):
    def setUp(self):
        from datetime import timedelta
        from apps.branch.models import Branch
        from django.utils import timezone as tz
        from apps.lead.models import LeadCall

        self.admin = User.objects.create_user(
            email='admin@test.com', name='Admin', password='pass', is_staff=True,
        )
        self.counselor = User.objects.create_user(
            email='c9@test.com', name='Counselor Nine', password='pass',
            is_staff=True,
        )
        self.client.force_authenticate(self.admin)
        self.branch = Branch.objects.create(name='Biratnagar', code='BRT')
        self.lead = Lead.objects.create(full_name='Call Person', phone='9811111111')
        from apps.student.models import Student
        self.student = Student.objects.create(
            full_name='Call Person', phone='9811111111', lead=self.lead,
        )
        now = tz.now()
        self.recent_call = LeadCall.objects.create(
            lead=self.lead, call_timestamp=now, direction='outbound',
            caller='Counselor Nine', phone_number='9811111111',
            duration_seconds=270, outcome='interested',
            created_by=self.counselor, branch=self.branch,
        )
        self.old_call = LeadCall.objects.create(
            lead=self.lead, call_timestamp=now - timedelta(days=10),
            direction='inbound', caller='Someone Else',
            phone_number='9822222222', duration_seconds=60,
            created_by=self.admin, branch=self.branch,
        )

    def _export(self, query=''):                      
        response = self.client.get(f'/api/calls/export/{query}')
        self.assertEqual(response.status_code, 200)
        raw = b''.join(response.streaming_content)
        rows = list(csv.reader(io.StringIO(raw.decode('utf-8-sig'))))
        return response, raw, rows

    def test_list_returns_all_calls_newest_first(self):
        response = self.client.get('/api/calls/')
        self.assertEqual(response.status_code, 200)
        ids = [r['id'] for r in response.data['results']]
        self.assertEqual(ids, [self.recent_call.id, self.old_call.id])
        row = response.data['results'][0]
        self.assertEqual(row['lead_name'], 'Call Person')
        self.assertTrue(row['lead_reference_number'].startswith('LD-'))

    def test_filter_direction(self):
        response = self.client.get('/api/calls/?direction=inbound')
        self.assertEqual([r['id'] for r in response.data['results']], [self.old_call.id])

    def test_filter_created_by_user(self):
        response = self.client.get(f'/api/calls/?created_by={self.counselor.id}')
        self.assertEqual(
            [r['id'] for r in response.data['results']], [self.recent_call.id],
        )

    def test_filter_by_student(self):
        response = self.client.get(f'/api/calls/?student={self.student.id}')
        self.assertEqual(response.data['count'], 2)

    def test_filter_date_range(self):
        from django.utils import timezone as tz
        today = tz.now().date().isoformat()
        response = self.client.get(f'/api/calls/?call_timestamp__gte={today}')
        self.assertEqual(
            [r['id'] for r in response.data['results']], [self.recent_call.id],
        )

    def test_search(self):
        response = self.client.get('/api/calls/?search=Counselor Nine')
        self.assertEqual(response.data['count'], 1)
        search_lead = self.client.get('/api/calls/?search=Call Person')
        self.assertEqual(search_lead.data['count'], 2)

    def test_branch_scoping_for_non_staff(self):
        from apps.branch.models import Branch
        from apps.lead.models import LeadCall
        other_branch = Branch.objects.create(name='Dharan', code='DHR')
        other_admin = User.objects.create_user(
            email='br@test.com', name='BR User', password='x',
            branch=other_branch,
        )
        LeadCall.objects.filter(id=self.recent_call.id).update(branch=None)
        self.client.force_authenticate(other_admin)
        response = self.client.get('/api/calls/')
        self.assertNotIn(self.recent_call.id, [r['id'] for r in response.data['results']])

    def test_export_headers_and_row_count(self):
        response, raw, rows = self._export()
        self.assertTrue(response['Content-Type'].startswith('text/csv'))
        self.assertIn('calls-', response['Content-Disposition'])
        self.assertTrue(raw.startswith(b'\xef\xbb\xbf'))        
        self.assertEqual(rows[0], [
            'Date & time (NPT)', 'Direction', 'Lead', 'Reference', 'Phone',
            'Caller', 'Logged by', 'Duration (MM:SS)', 'Seconds',
            'Outcome', 'Notes', 'Recording',
        ])
        self.assertEqual(len(rows) - 1, 2)                      

    def test_export_respects_filters(self):
        _, _, rows = self._export('?direction=inbound')
        self.assertEqual(len(rows) - 1, 1)
        self.assertEqual(rows[1][1], 'inbound')

    def test_export_neutralises_formula_in_notes(self):
        from apps.lead.models import LeadCall
        from django.utils import timezone as tz
        LeadCall.objects.create(
            lead=self.lead, call_timestamp=tz.now(), direction='outbound',
            caller='X', phone_number='9833333333', duration_seconds=5,
            notes='=HYPERLINK("http://evil","click")',
            created_by=self.admin, branch=self.branch,
        )
        _, _, rows = self._export()
        notes = [r[10] for r in rows[1:]]
        self.assertIn('\'=HYPERLINK("http://evil","click")', notes)

    def test_export_duration_columns(self):
        _, _, rows = self._export()
        by_seconds = {r[8]: r[7] for r in rows[1:]}
        self.assertEqual(by_seconds['270'], '04:30')
        self.assertEqual(by_seconds['60'], '01:00')

    def test_export_query_count_is_constant(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        from apps.lead.models import LeadCall
        from django.utils import timezone as tz

        with CaptureQueriesContext(connection) as small:
            self._export()

        for i in range(25):
            LeadCall.objects.create(
                lead=self.lead, call_timestamp=tz.now(), direction='outbound',
                caller='Bulk', phone_number=f'98000000{i:02d}', duration_seconds=i,
                created_by=self.counselor, branch=self.branch,
            )
        with CaptureQueriesContext(connection) as large:
            self._export()

        self.assertEqual(len(small), len(large))

class CallDateFilterTests(APITestCase):
    """date_from / date_to filters use Nepal-time day boundaries."""

    def setUp(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from apps.branch.models import Branch
        from apps.lead.models import LeadCall

        npt = ZoneInfo("Asia/Kathmandu")

        self.admin = User.objects.create_user(
            email='datefilter@test.com', name='Date Admin',
            password='pass', is_staff=True,
        )
        self.client.force_authenticate(self.admin)
        self.branch = Branch.objects.create(name='Kathmandu', code='KTM')
        self.lead = Lead.objects.create(
            full_name='Date Person', phone='9800000000',
        )

        def make_call(ts):
            return LeadCall.objects.create(
                lead=self.lead, call_timestamp=ts, direction='outbound',
                caller='Date Admin', phone_number='9800000000',
                duration_seconds=60, created_by=self.admin,
                branch=self.branch,
            )

        self.first_second = make_call(datetime(2026, 11, 23, 0, 0, 0, tzinfo=npt))
        self.midday = make_call(datetime(2026, 11, 23, 12, 0, 0, tzinfo=npt))
        self.last_second = make_call(
            datetime(2026, 11, 23, 23, 59, 59, 999999, tzinfo=npt)
        )
        self.day_before = make_call(
            datetime(2026, 11, 22, 23, 59, 59, tzinfo=npt)
        )
        self.next_day = make_call(datetime(2026, 11, 24, 0, 0, 0, tzinfo=npt))

    def _ids(self, response):
        return {row['id'] for row in response.data['results']}

    def test_single_day_includes_both_edges_and_excludes_neighbours(self):
        response = self.client.get(
            '/api/calls/?date_from=2026-11-23&date_to=2026-11-23'
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self._ids(response),
            {self.first_second.id, self.midday.id, self.last_second.id},
        )
        self.assertEqual(response.data['count'], 3)

    def test_date_from_only(self):
        response = self.client.get('/api/calls/?date_from=2026-11-23')
        self.assertEqual(
            self._ids(response),
            {self.first_second.id, self.midday.id,
             self.last_second.id, self.next_day.id},
        )

    def test_date_to_only(self):
        response = self.client.get('/api/calls/?date_to=2026-11-23')
        self.assertEqual(
            self._ids(response),
            {self.day_before.id, self.first_second.id,
             self.midday.id, self.last_second.id},
        )

    def test_multi_day_range(self):
        response = self.client.get(
            '/api/calls/?date_from=2026-11-23&date_to=2026-11-24'
        )
        self.assertEqual(
            self._ids(response),
            {self.first_second.id, self.midday.id,
             self.last_second.id, self.next_day.id},
        )

    def test_no_params_returns_everything(self):
        response = self.client.get('/api/calls/')
        self.assertEqual(response.data['count'], 5)

class GlobalCallSummaryTests(APITestCase):
    def setUp(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from apps.branch.models import Branch
        from apps.lead.models import LeadCall

        self.npt = ZoneInfo("Asia/Kathmandu")
        self.brt = Branch.objects.create(name='Biratnagar', code='BRT')
        self.dhr = Branch.objects.create(name='Dharan', code='DHR')

        self.admin = User.objects.create_user(
            email='sadmin@test.com', name='Staff Admin', password='x', is_staff=True,
        )
        self.brt_user = User.objects.create_user(
            email='brt@test.com', name='BRT User', password='x', branch=self.brt,
        )
        self.dhr_user = User.objects.create_user(
            email='dhr@test.com', name='DHR User', password='x', branch=self.dhr,
        )
        self.lead = Lead.objects.create(full_name='Sum Person', phone='9800000001')

        def make(ts, direction, dur, user, branch):
            return LeadCall.objects.create(
                lead=self.lead, call_timestamp=ts, direction=direction,
                caller=user.name, phone_number='9800000001',
                duration_seconds=dur, created_by=user, branch=branch,
            )

        npt = self.npt
        # Biratnagar: 3 calls, two on different NPT days around midnight
        make(datetime(2026, 11, 23, 23, 30, tzinfo=npt), 'outbound', 259, self.brt_user, self.brt)
        make(datetime(2026, 11, 24, 0, 30, tzinfo=npt), 'inbound', 0, self.brt_user, self.brt)
        make(datetime(2026, 11, 24, 10, 0, tzinfo=npt), 'outbound', 120, self.brt_user, self.brt)
        # Dharan: 2 calls
        make(datetime(2026, 11, 24, 11, 0, tzinfo=npt), 'outbound', 60, self.dhr_user, self.dhr)
        make(datetime(2026, 11, 24, 12, 0, tzinfo=npt), 'inbound', 30, self.dhr_user, self.dhr)

    def test_totals_match_list_count(self):
        self.client.force_authenticate(self.admin)
        qs = '?direction=outbound&date_from=2026-11-23'
        summary = self.client.get(f'/api/calls/summary/{qs}').data
        listing = self.client.get(f'/api/calls/{qs}').data
        self.assertEqual(summary['total_calls'], listing['count'])
        self.assertEqual(summary['total_calls'], 3)

    def test_day_buckets_use_nepal_time(self):
        self.client.force_authenticate(self.brt_user)
        data = self.client.get('/api/calls/summary/').data
        days = {row['date']: row['count'] for row in data['per_day']}
        self.assertEqual(days, {'2026-11-23': 1, '2026-11-24': 2})

    def test_empty_result_returns_zeros(self):
        self.client.force_authenticate(self.admin)
        data = self.client.get('/api/calls/summary/?search=zzzzzz').data
        self.assertEqual(data['total_calls'], 0)
        self.assertEqual(data['inbound'], 0)
        self.assertEqual(data['outbound'], 0)
        self.assertEqual(data['talk_seconds'], 0)
        self.assertEqual(data['avg_seconds'], 0)
        self.assertEqual(data['connect_rate'], 0)
        self.assertEqual(data['per_day'], [])
        self.assertEqual(data['top_callers'], [])

    def test_non_staff_sees_only_own_branch(self):
        self.client.force_authenticate(self.brt_user)
        self.assertEqual(self.client.get('/api/calls/summary/').data['total_calls'], 3)

        self.client.force_authenticate(self.dhr_user)
        self.assertEqual(self.client.get('/api/calls/summary/').data['total_calls'], 2)

        self.client.force_authenticate(self.dhr_user)
        data = self.client.get(f'/api/calls/summary/?branch_id={self.brt.id}').data
        self.assertEqual(data['total_calls'], 2)

        self.client.force_authenticate(self.admin)
        self.assertEqual(self.client.get('/api/calls/summary/').data['total_calls'], 5)

        data = self.client.get(f'/api/calls/summary/?branch_id={self.dhr.id}').data
        self.assertEqual(data['total_calls'], 2)

    def test_query_count_is_fixed(self):
        from datetime import datetime
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        from apps.lead.models import LeadCall

        self.client.force_authenticate(self.admin)

        with CaptureQueriesContext(connection) as before:
            self.client.get('/api/calls/summary/')
            baseline = len(before)

            for i in range(20):
                LeadCall.objects.create(
                    lead=self.lead,
                    call_timestamp=datetime(2026, 11, 25, 9, i, tzinfo=self.npt),
                    direction='outbound', caller='Bulk', phone_number='9800000001',
                    duration_seconds=30, created_by=self.brt_user, branch=self.brt,
                )

            with self.assertNumQueries(baseline):
                response = self.client.get('/api/calls/summary/')
            self.assertEqual(response.data['total_calls'], 25)