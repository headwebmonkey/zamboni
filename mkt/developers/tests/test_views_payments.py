import json

from django.core.exceptions import ObjectDoesNotExist

import mock
from mock import ANY
from nose.tools import eq_, ok_, raises
from pyquery import PyQuery as pq

import amo
import amo.tests
from amo.urlresolvers import reverse
from addons.models import (Addon, AddonCategory, AddonDeviceType,
                           AddonUpsell, AddonUser, Category)
from constants.payments import (PAYMENT_METHOD_ALL,
                                PAYMENT_METHOD_CARD,
                                PAYMENT_METHOD_OPERATOR)
from mkt.constants.payments import ACCESS_PURCHASE, ACCESS_SIMULATE
from mkt.constants.regions import ALL_PAID_REGION_IDS
from market.models import Price
from users.models import UserProfile

import mkt
from mkt.developers.models import (AddonPaymentAccount, PaymentAccount,
                                   SolitudeSeller, uri_to_pk, UserInappKey)
from mkt.site.fixtures import fixture
from mkt.webapps.models import AddonExcludedRegion as AER, ContentRating


def setup_payment_account(app, user, uid='uid'):
    seller = SolitudeSeller.objects.create(user=user, uuid=uid)
    payment = PaymentAccount.objects.create(user=user, solitude_seller=seller,
                                            agreed_tos=True, seller_uri=uid,
                                            uri=uid)
    return AddonPaymentAccount.objects.create(addon=app,
        product_uri='/path/to/%s/' % app.pk, payment_account=payment)


class InappTest(amo.tests.TestCase):

    def setUp(self):
        self.create_switch('in-app-payments')
        self.app = Addon.objects.get(pk=337141)
        self.app.update(premium_type=amo.ADDON_FREE_INAPP)
        self.user = UserProfile.objects.get(pk=31337)
        self.other = UserProfile.objects.get(pk=999)
        self.login(self.user)
        self.account = setup_payment_account(self.app, self.user)
        self.url = reverse('mkt.developers.apps.in_app_config',
                           args=[self.app.app_slug])

    def set_mocks(self, solitude):
        get = mock.Mock()
        get.get_object_or_404.return_value = {
            'seller_product': '/path/to/prod-pk/'
        }
        post = mock.Mock()
        post.return_value = get
        solitude.api.bango.product = post

        get = mock.Mock()
        get.get_object_or_404.return_value = {'resource_pk': 'some-key',
                                              'secret': 'shhh!'}
        post = mock.Mock()
        post.return_value = get
        solitude.api.generic.product = post


@mock.patch('mkt.developers.views_payments.client')
class TestInappConfig(InappTest):
    fixtures = fixture('webapp_337141', 'user_999')

    @raises(ObjectDoesNotExist)
    def test_not_seller(self, solitude):
        post = mock.Mock()
        post.side_effect = ObjectDoesNotExist
        solitude.api.generic.product = post
        eq_(self.client.get(self.url).status_code, 404)

    def test_key_generation(self, solitude):
        self.set_mocks(solitude)
        self.client.post(self.url, {})
        args = solitude.api.generic.product().patch.call_args
        assert 'secret' in args[1]['data']

    def test_logged_out(self, solitude):
        self.client.logout()
        self.assertLoginRequired(self.client.get(self.url))

    def test_different(self, solitude):
        self.login(self.other)
        eq_(self.client.get(self.url).status_code, 403)

    def test_developer(self, solitude):
        self.login(self.other)
        AddonUser.objects.create(addon=self.app, user=self.other,
                                 role=amo.AUTHOR_ROLE_DEV)
        # Developer can read, but not reset.
        eq_(self.client.get(self.url).status_code, 200)
        eq_(self.client.post(self.url).status_code, 403)

    def test_not_inapp(self, solitude):
        self.app.update(premium_type=amo.ADDON_PREMIUM)
        eq_(self.client.get(self.url).status_code, 302)

    def test_no_account(self, solitude):
        self.app.app_payment_account.delete()
        eq_(self.client.get(self.url).status_code, 302)


@mock.patch('mkt.developers.views_payments.client')
class TestInappSecret(InappTest):
    fixtures = fixture('webapp_337141', 'user_999')

    def setUp(self):
        super(TestInappSecret, self).setUp()
        self.url = reverse('mkt.developers.apps.in_app_secret',
                           args=[self.app.app_slug])

    def test_show_secret(self, solitude):
        self.set_mocks(solitude)
        resp = self.client.get(self.url)
        eq_(resp.content, 'shhh!')
        pk = uri_to_pk(self.account.product_uri)
        solitude.api.bango.product.assert_called_with(pk)
        solitude.api.generic.product.assert_called_with('prod-pk')

    def test_logged_out(self, solitude):
        self.client.logout()
        self.assertLoginRequired(self.client.get(self.url))

    def test_different(self, solitude):
        self.client.login(username='regular@mozilla.com', password='password')
        eq_(self.client.get(self.url).status_code, 403)

    def test_developer(self, solitude):
        self.set_mocks(solitude)
        self.login(self.other)
        AddonUser.objects.create(addon=self.app, user=self.other,
                                 role=amo.AUTHOR_ROLE_DEV)
        resp = self.client.get(self.url)
        eq_(resp.content, 'shhh!')


class InappKeysTest(InappTest):
    fixtures = fixture('webapp_337141', 'user_999')

    def setUp(self):
        super(InappKeysTest, self).setUp()
        self.create_switch('in-app-sandbox')
        self.url = reverse('mkt.developers.apps.in_app_keys')
        self.seller_uri = '/seller/1/'
        self.product_pk = 2

    def setup_solitude(self, solitude):
        solitude.api.generic.seller.post.return_value = {
            'resource_uri': self.seller_uri}
        solitude.api.generic.product.post.return_value = {
            'resource_pk': self.product_pk}


@mock.patch('mkt.developers.models.client')
class TestInappKeys(InappKeysTest):

    def test_logged_out(self, solitude):
        self.client.logout()
        self.assertLoginRequired(self.client.get(self.url))

    def test_no_key(self, solitude):
        res = self.client.get(self.url)
        eq_(res.status_code, 200)
        eq_(res.context['key'], None)

    def test_key_generation(self, solitude):
        self.setup_solitude(solitude)
        res = self.client.post(self.url)

        ok_(res['Location'].endswith(self.url), res)
        ok_(solitude.api.generic.seller.post.called)
        ok_(solitude.api.generic.product.post.called)
        key = UserInappKey.objects.get()
        eq_(key.solitude_seller.resource_uri, self.seller_uri)
        eq_(key.seller_product_pk, self.product_pk)
        m = solitude.api.generic.product.post.mock_calls
        eq_(m[0][2]['data']['access'], ACCESS_SIMULATE)

    def test_reset(self, solitude):
        self.setup_solitude(solitude)
        key = UserInappKey.create(self.user)
        product = mock.Mock()
        solitude.api.generic.product.return_value = product

        self.client.post(self.url)
        product.patch.assert_called_with(data={'secret': ANY})
        solitude.api.generic.product.assert_called_with(key.seller_product_pk)


@mock.patch('mkt.developers.models.client')
class TestInappKeySecret(InappKeysTest):

    def setUp(self):
        super(TestInappKeySecret, self).setUp()

    def setup_objects(self, solitude):
        self.setup_solitude(solitude)
        key = UserInappKey.create(self.user)
        self.url = reverse('mkt.developers.apps.in_app_key_secret',
                           args=[key.pk])

    def test_logged_out(self, solitude):
        self.setup_objects(solitude)
        self.client.logout()
        self.assertLoginRequired(self.client.get(self.url))

    def test_different(self, solitude):
        self.setup_objects(solitude)
        self.login(self.other)
        eq_(self.client.get(self.url).status_code, 403)

    def test_secret(self, solitude):
        self.setup_objects(solitude)
        secret = 'not telling'
        product = mock.Mock()
        product.get.return_value = {'secret': secret}
        solitude.api.generic.product.return_value = product

        res = self.client.get(self.url)
        eq_(res.status_code, 200)
        eq_(res.content, secret)


class TestPayments(amo.tests.TestCase):
    fixtures = fixture('webapp_337141', 'user_999', 'group_admin',
                       'user_admin', 'user_admin_group', 'prices')

    def setUp(self):
        self.webapp = self.get_webapp()
        AddonDeviceType.objects.create(
            addon=self.webapp, device_type=amo.DEVICE_GAIA.id)
        self.url = self.webapp.get_dev_url('payments')

        self.user = UserProfile.objects.get(pk=31337)
        self.other = UserProfile.objects.get(pk=999)
        self.admin = UserProfile.objects.get(email='admin@mozilla.com')

        # Default to logging in as the app owner.
        self.login(self.user)
        self.price = Price.objects.filter()[0]
        self.patch = mock.patch('mkt.developers.models.client')
        self.sol = self.patch.start()
        prs = set([p['region'] for p in self.price.prices()])
        self.price_regions = (set(ALL_PAID_REGION_IDS).intersection(prs))

    def tearDown(self):
        self.patch.stop()

    def get_webapp(self):
        return Addon.objects.get(pk=337141)

    def get_region_list(self):
        return list(AER.objects.values_list('region', flat=True))

    def get_postdata(self, extension):
        base = {'regions': self.get_region_list(),
                'free_platforms': ['free-%s' % dt.class_name for dt in
                                   self.webapp.device_types],
                'paid_platforms': ['paid-%s' % dt.class_name for dt in
                                   self.webapp.device_types]}
        base.update(extension)
        return base

    @mock.patch('mkt.developers.forms.ALL_PAID_REGION_IDS', new=[11])
    def test_paid_app_has_correct_regions_when_loaded(self):
        self.webapp.update(premium_type=amo.ADDON_PREMIUM)
        price = Price.objects.get(pk=1)
        res = self.client.post(
            self.url, self.get_postdata({'allow_inapp': False,
                                         'price': price.pk}), follow=True)
        pqr = pq(res.content)
        eq_(len(pqr('input[type=checkbox][value=11]:not(:disabled)')), 1)

    def test_free(self):
        res = self.client.post(
            self.url, self.get_postdata({'toggle-paid': 'free'}), follow=True)
        eq_(self.get_webapp().premium_type, amo.ADDON_FREE)
        eq_(res.context['is_paid'], False)

    def test_premium_passes(self):
        self.webapp.update(premium_type=amo.ADDON_FREE)
        res = self.client.post(
            self.url, self.get_postdata({'toggle-paid': 'paid'}), follow=True)
        eq_(self.get_webapp().premium_type, amo.ADDON_PREMIUM)
        eq_(res.context['is_paid'], True)

    def test_check_api_url_in_context(self):
        self.webapp.update(premium_type=amo.ADDON_FREE)
        res = self.client.get(self.url)
        eq_(res.context['api_pricelist_url'],
            reverse('api_dispatch_list', kwargs={'resource_name': 'prices',
                                                 'api_name': 'webpay'}))

    def test_regions_display_free(self):
        self.webapp.update(premium_type=amo.ADDON_FREE)
        res = self.client.get(self.url)
        pqr = pq(res.content)
        eq_(len(pqr('#regions-island')), 1)
        eq_(len(pqr('#paid-regions-island')), 0)

    def test_regions_display_premium(self):
        self.webapp.update(premium_type=amo.ADDON_PREMIUM)
        res = self.client.get(self.url)
        pqr = pq(res.content)
        eq_(len(pqr('#regions-island')), 0)
        eq_(len(pqr('#paid-regions-island')), 1)

    def test_free_with_in_app_tier_id_in_content(self):
        price_tier_zero = Price.objects.create(price='0.00')
        self.webapp.update(premium_type=amo.ADDON_PREMIUM)
        res = self.client.get(self.url)
        pqr = pq(res.content)
        eq_(len(pqr('#region-list[data-tier-zero-id]')), 1)
        eq_(int(pqr('#region-list').attr(
            'data-tier-zero-id')), price_tier_zero.pk)

    def test_not_applicable_data_attr_in_content(self):
        self.webapp.update(premium_type=amo.ADDON_PREMIUM)
        res = self.client.get(self.url)
        pqr = pq(res.content)
        eq_(len(pqr('#region-list[data-not-applicable-msg]')), 1)

    def test_pay_method_ids_in_context(self):
        self.webapp.update(premium_type=amo.ADDON_PREMIUM)
        res = self.client.get(self.url)
        self.assertSetEqual(res.context['payment_methods'].keys(),
                            [PAYMENT_METHOD_ALL, PAYMENT_METHOD_CARD,
                             PAYMENT_METHOD_OPERATOR])

    def test_free_with_in_app_deletes_upsell(self):
        self.make_premium(self.webapp)
        new_upsell_app = Addon.objects.create(type=self.webapp.type,
            status=self.webapp.status, name='upsell-%s' % self.webapp.id,
            premium_type=amo.ADDON_FREE)
        new_upsell = AddonUpsell(premium=self.webapp)
        new_upsell.free = new_upsell_app
        new_upsell.save()
        assert self.webapp.upsold is not None
        self.client.post(
            self.url, self.get_postdata({'price': 'free',
                                         'allow_inapp': 'True',
                                         'regions': ALL_PAID_REGION_IDS}),
                                         follow=True)
        eq_(self.get_webapp().upsold, None)

    def test_premium_in_app_passes(self):
        self.webapp.update(premium_type=amo.ADDON_FREE)
        res = self.client.post(
            self.url, self.get_postdata({'toggle-paid': 'paid'}))
        self.assert3xx(res, self.url)
        res = self.client.post(
            self.url, self.get_postdata({'allow_inapp': True,
                                         'price': self.price.pk,
                                         'regions': self.price_regions}))
        self.assert3xx(res, self.url)
        eq_(self.get_webapp().premium_type, amo.ADDON_PREMIUM_INAPP)

    def test_later_then_free(self):
        self.webapp.update(premium_type=amo.ADDON_PREMIUM,
                           status=amo.STATUS_NULL,
                           highest_status=amo.STATUS_PENDING)
        res = self.client.post(
            self.url, self.get_postdata({'toggle-paid': 'free',
                                         'price': self.price.pk}))
        self.assert3xx(res, self.url)
        eq_(self.get_webapp().status, amo.STATUS_PENDING)

    def test_premium_price_initial_already_set(self):
        Price.objects.create(price='0.00')  # Make a free tier for measure.
        self.make_premium(self.webapp)
        r = self.client.get(self.url)
        eq_(pq(r.content)('select[name=price] option[selected]').attr('value'),
            str(self.webapp.premium.price.id))

    def test_premium_price_initial_use_default(self):
        Price.objects.create(price='10.00')  # Make one more tier.

        self.webapp.update(premium_type=amo.ADDON_FREE)
        res = self.client.post(
            self.url, self.get_postdata({'toggle-paid': 'paid'}), follow=True)
        pqr = pq(res.content)
        eq_(pqr('select[name=price] option[selected]').attr('value'),
            str(Price.objects.get(price='0.99').id))

    def test_starting_with_free_inapp_has_free_selected(self):
        self.webapp.update(premium_type=amo.ADDON_FREE_INAPP)
        res = self.client.get(self.url)
        pqr = pq(res.content)
        eq_(pqr('select[name=price] option[selected]').attr('value'), 'free')

    def test_made_free_inapp_has_free_selected(self):
        self.make_premium(self.webapp)
        res = self.client.post(
            self.url, self.get_postdata({'price': 'free',
                                         'allow_inapp': 'True'}), follow=True)
        pqr = pq(res.content)
        eq_(pqr('select[name=price] option[selected]').attr('value'), 'free')

    def test_free_with_inapp_without_account_is_incomplete(self):
        self.webapp.update(premium_type=amo.ADDON_FREE)
        # Toggle to paid
        self.client.post(
            self.url, self.get_postdata({'toggle-paid': 'paid'}))
        res = self.client.post(
            self.url, self.get_postdata({'price': 'free',
                                         'allow_inapp': 'True',
                                         'regions': ALL_PAID_REGION_IDS}))
        self.assert3xx(res, self.url)
        eq_(self.get_webapp().status, amo.STATUS_NULL)

    def test_paid_app_without_account_is_incomplete(self):
        self.webapp.update(premium_type=amo.ADDON_FREE)
        # Toggle to paid
        self.client.post(
            self.url, self.get_postdata({'toggle-paid': 'paid'}))
        res = self.client.post(
            self.url, self.get_postdata({'price': self.price.pk,
                                         'allow_inapp': 'False',
                                         'regions': self.price_regions}))
        self.assert3xx(res, self.url)
        eq_(self.get_webapp().status, amo.STATUS_NULL)

    def setup_payment_acct(self, make_owner, user=None, bango_id=123):
        # Set up Solitude return values.
        api = self.sol.api  # Set up Solitude return values.
        api.generic.product.get_object.side_effect = ObjectDoesNotExist
        api.generic.product.post.return_value = {'resource_uri': 'gpuri'}
        api.bango.product.get_object.side_effect = ObjectDoesNotExist
        api.bango.product.post.return_value = {
            'resource_uri': 'bpruri', 'bango_id': 123}

        if not user:
            user = self.user

        amo.set_user(user)

        if make_owner:
            # Make owner
            AddonUser.objects.create(addon=self.webapp,
                                     user=user, role=amo.AUTHOR_ROLE_OWNER)

        # Set up an existing bank account.
        seller = SolitudeSeller.objects.create(
            resource_uri='/path/to/sel', user=user, uuid='uuid-%s' % user.pk)
        acct = PaymentAccount.objects.create(
            user=user, uri='asdf-%s' % user.pk, name='test', inactive=False,
            seller_uri='suri-%s' % user.pk, solitude_seller=seller,
            bango_package_id=123, agreed_tos=True)
        return acct, api, user

    def is_owner(self, user):
        return (self.webapp.authors.filter(user=user,
                addonuser__role=amo.AUTHOR_ROLE_OWNER).exists())

    def test_associate_acct_to_app_free_inapp(self):
        acct, api, user = self.setup_payment_acct(make_owner=True)

        # Must be an app owner to change this.
        assert self.is_owner(user)

        # Associate account with app.
        self.make_premium(self.webapp)
        res = self.client.post(
            self.url, self.get_postdata({'price': 'free',
                                         'allow_inapp': 'True',
                                         'regions': ALL_PAID_REGION_IDS,
                                         'accounts': acct.pk}), follow=True)
        self.assertNoFormErrors(res)
        eq_(res.status_code, 200)
        eq_(self.webapp.app_payment_account.payment_account.pk, acct.pk)

    def test_associate_acct_to_app(self):
        self.make_premium(self.webapp, price=self.price.price)
        acct, api, user = self.setup_payment_acct(make_owner=True)
        # Must be an app owner to change this.
        assert self.is_owner(user)
        # Associate account with app.
        res = self.client.post(
            self.url, self.get_postdata({'price': self.price.pk,
                                         'accounts': acct.pk,
                                         'regions': self.price_regions}),
                                         follow=True)
        self.assertNoFormErrors(res)
        eq_(res.status_code, 200)
        eq_(self.webapp.app_payment_account.payment_account.pk, acct.pk)
        kw = api.bango.product.post.call_args[1]['data']
        ok_(kw['secret'], kw)
        kw = api.generic.product.post.call_args[1]['data']
        eq_(kw['access'], ACCESS_PURCHASE)

    def test_associate_acct_to_app_when_not_owner(self):
        self.make_premium(self.webapp, price=self.price.price)
        self.login(self.other)
        acct, api, user = self.setup_payment_acct(make_owner=False,
                                                  user=self.other)
        # Check we're not an owner before we start.
        assert not self.is_owner(user)

        # Attempt to associate account with app as non-owner.
        res = self.client.post(
            self.url, self.get_postdata({'accounts': acct.pk}), follow=True)
        # Non-owner posts are forbidden.
        eq_(res.status_code, 403)
        # Payment account shouldn't be set as we're not the owner.
        assert not (AddonPaymentAccount.objects
                                       .filter(addon=self.webapp).exists())

    def test_associate_acct_to_app_when_not_owner_and_an_admin(self):
        self.make_premium(self.webapp, self.price.price)
        self.login(self.admin)
        acct, api, user = self.setup_payment_acct(make_owner=False,
                                                  user=self.admin)
        # Check we're not an owner before we start.
        assert not self.is_owner(user)
        assert not (AddonPaymentAccount.objects
                                       .filter(addon=self.webapp).exists())
        # Attempt to associate account with app as non-owner admin.
        res = self.client.post(
            self.url, self.get_postdata({'accounts': acct.pk,
                                         'price': self.price.pk,
                                         'regions': self.price_regions}),
                                         follow=True)
        self.assertFormError(res, 'bango_account_list_form', 'accounts',
                             [u'You are not permitted to change payment '
                               'accounts.'])

        # Payment account shouldn't be set as we're not the owner.
        assert not (AddonPaymentAccount.objects
                                       .filter(addon=self.webapp).exists())
        pqr = pq(res.content)
        # Payment field should be disabled.
        eq_(len(pqr('#id_accounts[disabled]')), 1)
        # There's no existing associated account.
        eq_(len(pqr('.current-account')), 0)

    def test_associate_acct_to_app_when_admin_and_owner_acct_exists(self):
        self.make_premium(self.webapp, price=self.price.price)
        owner_acct, api, owner_user = self.setup_payment_acct(make_owner=True)

        assert self.is_owner(owner_user)

        res = self.client.post(
            self.url, self.get_postdata({'accounts': owner_acct.pk,
                                         'price': self.price.pk,
                                         'regions': self.price_regions}),
                                         follow=True)
        assert (AddonPaymentAccount.objects
                                   .filter(addon=self.webapp).exists())

        self.login(self.admin)
        admin_acct, api, admin_user = self.setup_payment_acct(make_owner=False,
                                                              user=self.admin)
        # Check we're not an owner before we start.
        assert not self.is_owner(admin_user)

        res = self.client.post(
            self.url, self.get_postdata({'accounts': admin_acct.pk,
                                         'price': self.price.pk,
                                         'regions': self.price_regions}),
                                         follow=True)

        self.assertFormError(res, 'bango_account_list_form', 'accounts',
                             [u'You are not permitted to change payment '
                               'accounts.'])


    def test_one_owner_and_a_second_one_sees_selected_plus_own_accounts(self):
        self.make_premium(self.webapp, price=self.price.price)
        owner_acct, api, owner = self.setup_payment_acct(make_owner=True)
        # Should be an owner.
        assert self.is_owner(owner)

        res = self.client.post(
            self.url, self.get_postdata({'accounts': owner_acct.pk,
                                         'price': self.price.pk,
                                         'regions': self.price_regions}),
                                         follow=True)
        assert (AddonPaymentAccount.objects
                                   .filter(addon=self.webapp).exists())

        # Login as other user.
        self.login(self.other)
        owner_acct2, api, owner2 = self.setup_payment_acct(make_owner=True,
                                                           user=self.other)
        assert self.is_owner(owner2)
        # Should see the saved account plus 2nd owner's own account select
        # and be able to save their own account but not the other owners.
        res = self.client.get(self.url)
        eq_(res.status_code, 200)
        pqr = pq(res.content)
        # Check we have just our account option present + '----'.
        eq_(len(pqr('#id_accounts option')), 2)
        eq_(len(pqr('#id_account[disabled]')), 0)
        eq_(pqr('.current-account').text(), unicode(owner_acct))

        res = self.client.post(
            self.url, self.get_postdata({'accounts': owner_acct2.pk,
                                         'price': self.price.pk,
                                         'regions': self.price_regions}),
                                         follow=True)
        eq_(res.status_code, 200)
        self.assertNoFormErrors(res)
        pqr = pq(res.content)
        eq_(len(pqr('.current-account')), 0)
        eq_(pqr('#id_accounts option[selected]').text(), unicode(owner_acct2))
        # Now there should just be our account.
        eq_(len(pqr('#id_accounts option')), 1)

    def test_existing_account_should_be_disabled_for_non_owner(self):
        self.make_premium(self.webapp, price=self.price.price)
        acct, api, user = self.setup_payment_acct(make_owner=True)
        # Must be an app owner to change this.
        assert self.is_owner(user)
        # Associate account with app.
        res = self.client.post(
            self.url, self.get_postdata({'accounts': acct.pk,
                                         'price': self.price.pk,
                                         'regions': self.price_regions}),
                                         follow=True)
        amo.set_user(self.other)
        # Make this user a dev so they have access to the payments page.
        AddonUser.objects.create(addon=self.webapp,
                                 user=self.other, role=amo.AUTHOR_ROLE_DEV)
        self.login(self.other)
        # Make sure not an owner.
        assert not self.is_owner(self.other)
        res = self.client.get(self.url)
        eq_(res.status_code, 200)
        pqr = pq(res.content)
        # No accounts setup.
        eq_(len(pqr('.no-accounts')), 1)
        # Currently associated account should be displayed separately.
        eq_(pqr('.current-account').text(), unicode(acct))

    def test_existing_account_should_be_disabled_for_non_owner_admin(self):
        self.make_premium(self.webapp, price=self.price.price)
        # Login as regular user
        self.login(self.other)
        owner_acct, api, user = self.setup_payment_acct(make_owner=True,
                                                        user=self.other)
        # Must be an app owner to change this.
        assert self.is_owner(self.other)
        # Associate account with app.
        res = self.client.post(self.url,
                  self.get_postdata({'accounts': owner_acct.pk,
                                     'price': self.price.pk,
                                     'regions': self.price_regions}),
                                     follow=True)
        self.assertNoFormErrors(res)
        # Login as admin.
        self.login(self.admin)
        # Create an account as an admin.
        admin_acct, api, admin_user = self.setup_payment_acct(make_owner=False,
                                                              user=self.admin)
        # Make sure not an owner.
        assert not self.is_owner(self.admin)
        res = self.client.get(self.url)
        eq_(res.status_code, 200)
        pqr = pq(res.content)
        # Payment field should be disabled.
        eq_(len(pqr('#id_accounts[disabled]')), 1)
        # Currently associated account should be displayed separately.
        eq_(pqr('.current-account').text(), unicode(owner_acct))


class TestRegions(amo.tests.TestCase):
    fixtures = ['base/apps', 'base/users', 'webapps/337141-steamcube']

    def setUp(self):
        self.webapp = self.get_webapp()
        AddonDeviceType.objects.create(
            addon=self.webapp, device_type=amo.DEVICE_GAIA.id)
        self.url = self.webapp.get_dev_url('payments')
        self.username = 'admin@mozilla.com'
        assert self.client.login(username=self.username, password='password')
        self.patch = mock.patch('mkt.developers.models.client')
        self.sol = self.patch.start()

    def tearDown(self):
        self.patch.stop()

    def get_webapp(self):
        return Addon.objects.get(pk=337141)

    def get_dict(self, **kwargs):
        extension = {'regions': mkt.regions.REGION_IDS,
                     'other_regions': 'on',
                     'free_platforms': ['free-%s' % dt.class_name for dt in
                                        self.webapp.device_types]}
        extension.update(kwargs)
        return extension

    def get_excluded_ids(self):
        return sorted(AER.objects.filter(addon=self.webapp)
                                 .values_list('region', flat=True))

    def test_edit_other_categories_are_not_excluded(self):
        # Keep the category around for good measure.
        Category.objects.create(type=amo.ADDON_WEBAPP, slug='games')

        r = self.client.post(self.url, self.get_dict())
        self.assertNoFormErrors(r)
        eq_(AER.objects.count(), 0)

    def test_brazil_games_form_disabled(self):
        games = Category.objects.create(type=amo.ADDON_WEBAPP, slug='games')
        AddonCategory.objects.create(addon=self.webapp, category=games)

        r = self.client.get(self.url, self.get_dict())
        self.assertNoFormErrors(r)

        td = pq(r.content)('#regions')
        eq_(td.find('div[data-disabled]').attr('data-disabled'),
            '[%d]' % mkt.regions.BR.id)
        eq_(td.find('.note.disabled-regions').length, 1)

    def test_brazil_games_form_enabled_with_content_rating(self):
        rb = mkt.regions.BR.ratingsbodies[0]
        ContentRating.objects.create(
            addon=self.webapp, ratings_body=rb.id, rating=rb.ratings[0].id)

        games = Category.objects.create(type=amo.ADDON_WEBAPP, slug='games')
        AddonCategory.objects.create(addon=self.webapp, category=games)

        r = self.client.get(self.url)
        td = pq(r.content)('#regions')
        eq_(td.find('div[data-disabled]').attr('data-disabled'), '[]')
        eq_(td.find('.note.disabled-regions').length, 0)

    def test_brazil_other_cats_form_enabled(self):
        r = self.client.get(self.url)
        td = pq(r.content)('#regions')
        eq_(td.find('div[data-disabled]').attr('data-disabled'), '[]')
        eq_(td.find('.note.disabled-regions').length, 0)


class PaymentsBase(amo.tests.TestCase):
    fixtures = fixture('user_editor', 'user_999')

    def setUp(self):
        self.user = UserProfile.objects.get(pk=999)
        self.login(self.user)
        self.account = self.create()

    def create(self):
        # If user is defined on SolitudeSeller, why do we also need it on
        # PaymentAccount? Fewer JOINs.
        seller = SolitudeSeller.objects.create(user=self.user)
        return PaymentAccount.objects.create(user=self.user,
                                             solitude_seller=seller,
                                             uri='/bango/package/123',
                                             name="cvan's cnotes",
                                             agreed_tos=True)


class TestPaymentAccountsAdd(PaymentsBase):
    # TODO: this test provides bare coverage and might need to be expanded.

    def setUp(self):
        super(TestPaymentAccountsAdd, self).setUp()
        self.url = reverse('mkt.developers.bango.add_payment_account')

    def test_login_required(self):
        self.client.logout()
        self.assertLoginRequired(self.client.post(self.url, data={}))

    @mock.patch('mkt.developers.models.client')
    def test_create(self, client):
        res = self.client.post(self.url, data={
            'bankAccountPayeeName': 'name',
            'companyName': 'company',
            'vendorName': 'vendor',
            'financeEmailAddress': 'a@a.com',
            'adminEmailAddress': 'a@a.com',
            'supportEmailAddress': 'a@a.com',
            'address1': 'address 1',
            'addressCity': 'city',
            'addressState': 'state',
            'addressZipCode': 'zip',
            'addressPhone': '123',
            'countryIso': 'BRA',
            'currencyIso': 'EUR',
            'bankAccountNumber': '123',
            'bankAccountCode': '123',
            'bankName': 'asd',
            'bankAddress1': 'address 2',
            'bankAddressZipCode': '123',
            'bankAddressIso': 'BRA',
            'account_name': 'account'
        })
        output = json.loads(res.content)
        ok_('pk' in output)
        ok_('agreement-url' in output)
        eq_(PaymentAccount.objects.count(), 2)


class TestPaymentAccounts(PaymentsBase):

    def setUp(self):
        super(TestPaymentAccounts, self).setUp()
        self.url = reverse('mkt.developers.bango.payment_accounts')

    def test_login_required(self):
        self.client.logout()
        self.assertLoginRequired(self.client.get(self.url))

    def test_mine(self):
        res = self.client.get(self.url)
        eq_(res.status_code, 200)
        output = json.loads(res.content)
        eq_(output[0]['id'], self.account.pk)
        ok_('&#39;' in output[0]['name'])  # Was jinja2 escaped.


class TestPaymentAccount(PaymentsBase):

    def setUp(self):
        super(TestPaymentAccount, self).setUp()
        self.url = reverse('mkt.developers.bango.payment_account',
                           args=[self.account.pk])

    def test_login_required(self):
        self.client.logout()
        self.assertLoginRequired(self.client.get(self.url))

    @mock.patch('mkt.developers.models.client')
    def test_get(self, client):
        package = mock.Mock()
        package.get.return_value = {'full': {'vendorName': 'testval'}}
        client.api.bango.package.return_value = package

        res = self.client.get(self.url)
        client.api.bango.package.assert_called_with('123')

        eq_(res.status_code, 200)
        output = json.loads(res.content)
        eq_(output['account_name'], self.account.name)
        assert 'vendorName' in output, (
            'Details from Bango not getting merged in: %s' % output)
        eq_(output['vendorName'], 'testval')


class TestPaymentAgreement(PaymentsBase):

    def setUp(self):
        super(TestPaymentAgreement, self).setUp()
        self.url = reverse('mkt.developers.bango.agreement',
                           args=[self.account.pk])

    def test_anon(self):
        self.client.logout()
        self.assertLoginRequired(self.client.get(self.url))

    @mock.patch('mkt.developers.views_payments.client.api')
    def test_get(self, api):
        api.bango.sbi.agreement.get_object.return_value = {
            'text': 'blah', 'valid': '2010-08-31T00:00:00'}
        res = self.client.get(self.url)
        eq_(res.status_code, 200)
        data = json.loads(res.content)
        eq_(data['text'], 'blah')

    @mock.patch('mkt.developers.views_payments.client.api')
    def test_set(self, api):
        api.bango.sbi.post.return_value = {
            'expires': '2014-08-31T00:00:00',
            'valid': '2014-08-31T00:00:00'}
        res = self.client.post(self.url)
        eq_(res.status_code, 200)
        data = json.loads(res.content)
        eq_(data['valid'], '2014-08-31T00:00:00')


class TestPaymentAccountsForm(PaymentsBase):

    def setUp(self):
        super(TestPaymentAccountsForm, self).setUp()
        self.url = reverse('mkt.developers.bango.payment_accounts_form')

    def test_login_required(self):
        self.client.logout()
        self.assertLoginRequired(self.client.get(self.url))

    def test_mine(self):
        res = self.client.get(self.url)
        eq_(res.status_code, 200)
        eq_(res.context['bango_account_list_form']
               .fields['accounts'].choices.queryset.get(), self.account)

    def test_mine_disagreed_tos(self):
        self.account.update(agreed_tos=False)
        res = self.client.get(self.url)
        eq_(res.status_code, 200)
        self.assertSetEqual(res.context['bango_account_list_form']
                               .fields['accounts'].choices.queryset.all(), [])


class TestPaymentDelete(PaymentsBase):

    def setUp(self):
        super(TestPaymentDelete, self).setUp()
        self.url = reverse('mkt.developers.bango.delete_payment_account',
                           args=[self.account.pk])

    def test_login_required(self):
        self.client.logout()
        self.assertLoginRequired(self.client.post(self.url, data={}))

    def test_not_mine(self):
        self.login(UserProfile.objects.get(pk=5497308))
        eq_(self.client.post(self.url, data={}).status_code, 404)

    def test_mine(self):
        eq_(self.client.post(self.url, data={}).status_code, 200)
        eq_(PaymentAccount.objects.get(pk=self.account.pk).inactive, True)
