"""
REST API list/detail endpoints only expose objects from the requesting user's groups.
Staff see everything.

Run from the repository root:
    python -m django test tests.django.test_api_group_scoping --settings=tests.django.settings_sqlite
"""
import tempfile

from django.contrib.auth.models import Group, User
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from rest_framework.test import APIClient

from Smartscope.core.models import (
    AtlasModel,
    AutoloaderGrid,
    Classifier,
    HighMagModel,
    HoleModel,
    Microscope,
    ScreeningSession,
    SquareModel,
)


def build_session_tree(group, microscope, label):
    """One session with one grid, atlas, square, hole, high-mag and a classifier on the square."""
    session = ScreeningSession(session=f'session_{label}', group=group, date='20260101', microscope_id=microscope)
    session.save()
    grid = AutoloaderGrid(position=1, name=f'grid_{label}', session_id=session)
    grid.save()
    atlas = AtlasModel(name=f'atlas_{label}', grid_id=grid)
    atlas.save()
    square = SquareModel(name=f'square_{label}', number=1, grid_id=grid, atlas_id=atlas)
    square.save()
    hole = HoleModel(name=f'hole_{label}', number=1, grid_id=grid, square_id=square, radius=1, area=1.0,
                     status='completed')
    hole.save()
    highmag = HighMagModel(name=f'highmag_{label}', number=1, grid_id=grid, hole_id=hole)
    highmag.save()
    classifier = Classifier(content_type=ContentType.objects.get_for_model(SquareModel),
                            object_id=square.pk, method_name='test', label='good')
    classifier.save()
    return dict(sessions=session, grids=grid, atlas=atlas, squares=square, holes=hole, highmag=highmag,
                classes=classifier)


# Field identifying each row in a list response. ClassifierSerializer excludes its id,
# so classifier rows are identified by the target they label.
ENDPOINT_KEYS = {
    'sessions': 'session_id',
    'grids': 'grid_id',
    'atlas': 'atlas_id',
    'squares': 'square_id',
    'holes': 'hole_id',
    'highmag': 'hm_id',
    'classes': 'object_id',
}


def response_keys(response, endpoint):
    """Identifying keys in a list response, paginated or not."""
    data = response.json()
    rows = data['results'] if isinstance(data, dict) else data
    return {str(row[ENDPOINT_KEYS[endpoint]]) for row in rows}


def expected_key(tree, endpoint):
    if endpoint == 'classes':
        return str(tree['classes'].object_id)
    return str(tree[endpoint].pk)


class ApiGroupScopingTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        microscope = Microscope(name='scope', location='lab', scope_path=tempfile.mkdtemp())
        microscope.save()
        cls.group_alpha = Group.objects.create(name='alpha')
        cls.group_beta = Group.objects.create(name='beta')
        cls.alpha = build_session_tree(cls.group_alpha, microscope, 'alpha')
        cls.beta = build_session_tree(cls.group_beta, microscope, 'beta')

        cls.alpha_user = User.objects.create_user('alpha_user')
        cls.alpha_user.groups.add(cls.group_alpha)
        cls.no_group_user = User.objects.create_user('no_group_user')
        cls.staff_user = User.objects.create_user('staff_user', is_staff=True)

    def client_for(self, user):
        client = APIClient()
        client.force_authenticate(user=user)
        return client

    def list_pks(self, user, endpoint, query=''):
        response = self.client_for(user).get(f'/api/{endpoint}/{query}')
        self.assertEqual(response.status_code, 200, f'{endpoint}: {response.content[:500]}')
        return response_keys(response, endpoint)

    def test_non_staff_lists_only_their_groups_objects(self):
        for endpoint in ENDPOINT_KEYS:
            with self.subTest(endpoint=endpoint):
                pks = self.list_pks(self.alpha_user, endpoint)
                self.assertEqual(pks, {expected_key(self.alpha, endpoint)})

    def test_staff_lists_every_groups_objects(self):
        for endpoint in ENDPOINT_KEYS:
            with self.subTest(endpoint=endpoint):
                pks = self.list_pks(self.staff_user, endpoint)
                self.assertEqual(pks, {expected_key(self.alpha, endpoint), expected_key(self.beta, endpoint)})

    def test_user_without_groups_sees_nothing(self):
        for endpoint in ENDPOINT_KEYS:
            with self.subTest(endpoint=endpoint):
                self.assertEqual(self.list_pks(self.no_group_user, endpoint), set())

    def test_session_without_group_is_hidden_from_non_staff(self):
        # Saving a session without a group fails in its directory signal, so clear it afterwards.
        orphan = ScreeningSession(session='orphan', group=self.group_alpha, date='20260101',
                                  microscope_id=self.alpha['sessions'].microscope_id)
        orphan.save()
        ScreeningSession.objects.filter(pk=orphan.pk).update(group=None)
        self.assertNotIn(orphan.pk, self.list_pks(self.alpha_user, 'sessions'))
        self.assertIn(orphan.pk, self.list_pks(self.staff_user, 'sessions'))

    def test_filters_and_pagination_still_apply(self):
        own_session = self.alpha['sessions'].pk
        other_session = self.beta['sessions'].pk
        self.assertEqual(self.list_pks(self.alpha_user, 'grids', f'?session_id={own_session}'),
                         {self.alpha['grids'].pk})
        self.assertEqual(self.list_pks(self.alpha_user, 'grids', f'?session_id={other_session}'), set())
        self.assertEqual(self.list_pks(self.staff_user, 'squares', f'?grid_id__session_id={other_session}'),
                         {self.beta['squares'].pk})
        self.assertEqual(self.list_pks(self.staff_user, 'sessions', '?all'),
                         {own_session, other_session})
        response = self.client_for(self.staff_user).get('/api/sessions/?page_size=1')
        self.assertEqual(response.json()['count'], 2)
        self.assertEqual(len(response.json()['results']), 1)

    def test_detail_of_other_groups_object_is_not_found(self):
        client = self.client_for(self.alpha_user)
        for endpoint in ENDPOINT_KEYS:
            with self.subTest(endpoint=endpoint):
                own = client.get(f'/api/{endpoint}/{self.alpha[endpoint].pk}/')
                self.assertEqual(own.status_code, 200, own.content[:500])
                other = client.get(f'/api/{endpoint}/{self.beta[endpoint].pk}/')
                self.assertEqual(other.status_code, 404)

    def test_custom_list_actions_are_scoped(self):
        pks = self.list_pks(self.alpha_user, 'squares', 'detailed/')
        self.assertEqual(pks, {self.alpha['squares'].pk})
        pks = self.list_pks(self.alpha_user, 'holes', 'simple/')
        self.assertEqual(pks, {self.alpha['holes'].pk})

    def test_delete_many_cannot_touch_other_groups(self):
        other = self.beta['sessions'].pk
        response = self.client_for(self.alpha_user).post('/api/sessions/delete_many/', [other], format='json')
        self.assertEqual(response.status_code, 204)
        self.assertTrue(ScreeningSession.objects.filter(pk=other).exists())

    def test_update_many_cannot_touch_other_groups(self):
        other = self.beta['squares'].pk
        response = self.client_for(self.alpha_user).patch(
            '/api/squares/update_many/', dict(uids=[other], selected=True), format='json')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(SquareModel.objects.get(pk=other).selected)
