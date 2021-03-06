from django.contrib.admin.views.decorators import staff_member_required, user_passes_test
from django.core.serializers.json import DjangoJSONEncoder
from django.forms.models import model_to_dict
from django.http import HttpResponse, HttpResponseServerError, JsonResponse
from django.shortcuts import render, redirect
from django.template.response import TemplateResponse
from django.template.loader import render_to_string
from django.views.decorators.csrf import csrf_exempt
from django.db.models import Q, Max
from django.utils import timezone
from django.conf import settings
try:
    from django.urls import reverse
except ImportError:
    from django.core.urlresolvers import reverse

import time
from datetime import datetime, date
from decimal import *
from operator import itemgetter
import itertools
import os
import json
import random
import string
import logging

from .models import *
from .payments import chargePayment
from .pushy import PushyAPI
from .emails import *
from .printing import Main as printing_main

# Create your views here.
logger = logging.getLogger("django.request")

def index(request):
    try:
        event = Event.objects.get(default=True)
    except Event.DoesNotExist:
        return render(request, 'registration/docs/no-event.html')


    tz = timezone.get_current_timezone()
    today = tz.localize(datetime.now())
    context = { 'event' : event }
    if event.attendeeRegStart <= today <= event.attendeeRegEnd:
        return render(request, 'registration/registration-form.html', context)
    return render(request, 'registration/closed.html', context)

def flush(request):
    request.session.flush()
    return JsonResponse({'success': True})

# Generator to require a user to be part of a group to access a view
# e.g. apply with:
#   @user_passes_test(in_group('Manager'))
# to require user to be a member of the 'Manager' group to continue
def in_group(groupname):
    def inner(user):
        return user.groups.filter(name=groupname).exists()
    return inner


###################################
# Payments

def doCheckout(billingData, total, discount, cartItems, orderItems, donationOrg, donationCharity, ip):
    event = Event.objects.get(default=True)
    reference = getConfirmationToken()
    while Order.objects.filter(reference=reference).exists():
        reference = getConfirmationToken()
        
    order = Order(total=Decimal(total), reference=reference, discount=discount,
                  orgDonation=donationOrg, charityDonation=donationCharity)

    # Address collection is marked as required by event
    if event.collectBillingAddress:
        try:
            order.billingName="{0} {1}".format(billingData['cc_firstname'], billingData['cc_lastname'])
            order.billingAddress1=billingData['address1']
            order.billingAddress2=billingData['address2']
            order.billingCity=billingData['city']
            order.billingState=billingData['state']
            order.billingCountry=billingData['country']
            order.billingEmail=billingData['email']
        except KeyError as e:
            abort(400, "Address collection is required, but request is missing required field: {0}".format(e))

    # Otherwise, no need for anything except postal code (Square US/CAN)
    try:
        card_data = billingData['card_data']
        order.billingPostal = card_data['billing_postal_code']
        order.lastFour = card_data['last_4']
    except KeyError as e:
        abort(400, "A required field was missing from billingData: {0}".format(e))
        
    status, response = chargePayment(order, billingData, ip)

    if status:
        if cartItems:
            for item in cartItems:
                orderItem = saveCart(item)
                orderItem.order = order
                orderItem.save()
        elif orderItems:
            for oitem in orderItems:
                oitem.order = order
                oitem.save()
        order.status = 'Paid'
        order.save()
        if discount:
            discount.used = discount.used + 1
            discount.save()
        return True, "", order

    return False, response, order


def doZeroCheckout(discount, cartItems, orderItems):
    if cartItems:
        attendee = json.loads(cartItems[0].formData)['attendee']
        billingName = "{firstName} {lastName}".format(**attendee)
        billingEmail = attendee['email']
    elif orderItems:
        attendee = orderItems[0].badge.attendee
        billingName = "{0} {1}".format(attendee.firstName, attendee.lastName)
        billingEmail = attendee.email


    reference = getConfirmationToken()
    while Order.objects.filter(reference=reference).count() > 0:
        reference = getConfirmationToken()

    logger.debug(attendee)
    order = Order(total=0, reference=reference, discount=discount,
                  orgDonation=0, charityDonation=0, status="Complete",
                  billingType=Order.COMP, billingEmail=billingEmail,
                  billingName=billingName)
    order.save()

    if cartItems:
        for item in cartItems:
            orderItem = saveCart(item)
            orderItem.order = order
            orderItem.save()
    elif orderItems:
        for oitem in orderItems:
            oitem.order = order
            oitem.save()

    if discount:
        discount.used = discount.used + 1
        discount.save()
    return True, "", order


def getCartItemOptionTotal(options):
    optionTotal = 0
    for option in options:
        optionData = PriceLevelOption.objects.get(id=option['id'])
        if optionData.optionExtraType == 'int':
            if option['value']:
                optionTotal += (optionData.optionPrice*Decimal(option['value']))
        else:
            optionTotal += optionData.optionPrice
    return optionTotal

def getOrderItemOptionTotal(options):
    optionTotal = 0
    for option in options:
        if option.option.optionExtraType == 'int':
            if option.optionValue:
                optionTotal += (option.option.optionPrice*Decimal(option.optionValue))
        else:
            optionTotal += option.option.optionPrice
    return optionTotal

def getDiscountTotal(disc, subtotal):
    discount = Discount.objects.get(codeName=disc)
    if discount.isValid():
        if discount.amountOff:
            return discount.amountOff
        elif discount.percentOff:
            return Decimal(float(subtotal) * float(discount.percentOff)/100)



def getTotal(cartItems, orderItems, disc = ""):
    total = 0
    total_discount = 0
    if not cartItems and not orderItems:
        return 0, 0
    for item in cartItems:
        postData = json.loads(str(item.formData))
        pdp = postData['priceLevel']
        priceLevel = PriceLevel.objects.get(id=pdp['id'])
        itemTotal = priceLevel.basePrice

        options = pdp['options']
        itemTotal += getCartItemOptionTotal(options)

        if disc:
            discount = getDiscountTotal(disc, itemTotal)
            total_discount += discount
            itemTotal -= discount

        if itemTotal > 0:
            total += itemTotal

    for item in orderItems:
        itemSubTotal = item.priceLevel.basePrice
        effLevel = item.badge.effectiveLevel()
        # FIXME Why was this here?
        #if effLevel:
        #    itemTotal = itemSubTotal - effLevel.basePrice
        #else:
        itemTotal = itemSubTotal

        itemTotal += getOrderItemOptionTotal(item.attendeeoptions_set.all())

        if disc:
            discount = getDiscountTotal(disc, itemTotal)
            total_discount += discount
            itemTotal -= discount

        # FIXME Why?
        if itemTotal > 0:
            total += itemTotal

    return total, total_discount

def getDealerTotal(orderItems, discount, dealer):
    itemSubTotal = 0
    for item in orderItems:
        itemSubTotal = item.priceLevel.basePrice
        for option in item.attendeeoptions_set.all():
            if option.option.optionExtraType == 'int':
                if option.optionValue:
                    itemSubTotal += (option.option.optionPrice*Decimal(option.optionValue))
            else:
                itemSubTotal += option.option.optionPrice
    partnerCount = dealer.getPartnerCount()
    partnerBreakfast = 0
    if partnerCount > 0 and dealer.asstBreakfast:
      partnerBreakfast = 60*partnerCount
    wifi = 0
    power = 0
    if dealer.needWifi:
        wifi = 50
    if dealer.needPower:
        power = 15
    paidTotal = dealer.paidTotal()
    if discount:
        itemSubTotal = getDiscountTotal(discount, itemSubTotal)
    total = itemSubTotal + 45*partnerCount + partnerBreakfast + dealer.tableSize.basePrice + wifi + power - dealer.discount - paidTotal
    if total < 0:
      return 0
    return total

def applyDiscount(request):
    dis = request.session.get('discount', "")
    if dis:
        return JsonResponse({'success': False, 'message': 'Only one discount is allowed per order.'})

    try:
        postData = json.loads(request.body)
    except ValueError as e:
        logger.error("Unable to decode JSON for applyDiscount()")
        return JsonResponse({'success': False})
    dis = postData['discount']

    discount = Discount.objects.filter(codeName=dis)
    if discount.count() == 0:
        return JsonResponse({'success': False, 'message': 'That discount is not valid.'})
    discount = discount.first()
    if not discount.isValid():
        return JsonResponse({'success': False, 'message': 'That discount is not valid.'})

    request.session["discount"] = discount.codeName
    return JsonResponse({'success': True})


###################################
# New Staff

def newStaff(request, guid):
    event = Event.objects.get(default=True)
    context = {'token': guid, 'event': event}
    return render(request, 'registration/staff/staff-new.html', context)

def findNewStaff(request):
  try:
    postData = json.loads(request.body)
    email = postData['email']
    token = postData['token']

    token = TempToken.objects.get(email__iexact=email, token=token)
    if not token:
        return HttpResponseServerError("No Staff Found")

    if token.validUntil < timezone.now():
        return HttpResponseServerError("Invalid Token")
    if token.used:
        return HttpResponseServerError("Token Used")

    request.session["newStaff"] = token.token

    return JsonResponse({'success': True, 'message':'STAFF'})
  except Exception as e:
    logger.exception("Unable to find staff." + request.body)
    return HttpResponseServerError(str(e))

def infoNewStaff(request):
    event = Event.objects.get(default=True)
    try:
      tokenValue = request.session["newStaff"]
      token = TempToken.objects.get(token=tokenValue)
    except Exception as e:
      token = None
    context = {'staff': None, 'event': event, 'token': token}
    return render(request, 'registration/staff/staff-new-payment.html', context)

def addNewStaff(request):
    postData = json.loads(request.body)
    #create attendee from request post
    pda = postData['attendee']
    pds = postData['staff']
    pdp = postData['priceLevel']
    evt = postData['event']

    if evt:
      event = Event.objects.get(name=evt)
    else:
      event = Event.objects.get(default=True)

    tz = timezone.get_current_timezone()
    birthdate = tz.localize(datetime.strptime(pda['birthdate'], '%Y-%m-%d' ))

    attendee = Attendee(firstName=pda['firstName'], lastName=pda['lastName'], address1=pda['address1'], address2=pda['address2'],
                        city=pda['city'], state=pda['state'], country=pda['country'], postalCode=pda['postal'],
                        phone=pda['phone'], email=pda['email'], birthdate=birthdate,
                        emailsOk=True, surveyOk=False)
    attendee.save()

    badge = Badge(attendee=attendee, event=event, badgeName=pda['badgeName'])
    badge.save()

    shirt = ShirtSizes.objects.get(id=pds['shirtsize'])

    staff = Staff(attendee=attendee, event=event)
    staff.twitter = pds['twitter']
    staff.telegram = pds['telegram']
    staff.shirtsize = shirt
    staff.specialSkills = pds['specialSkills']
    staff.specialFood = pds['specialFood']
    staff.specialMedical = pds['specialMedical']
    staff.contactName = pds['contactName']
    staff.contactPhone = pds['contactPhone']
    staff.contactRelation = pds['contactRelation']
    staff.save()

    priceLevel = PriceLevel.objects.get(id=int(pdp['id']))

    orderItem = OrderItem(badge=badge, priceLevel=priceLevel, enteredBy="WEB")
    orderItem.save()

    for option in pdp['options']:
        plOption = PriceLevelOption.objects.get(id=int(option['id']))
        attendeeOption = AttendeeOptions(option=plOption, orderItem=orderItem, optionValue=option['value'])
        attendeeOption.save()

    orderItems = request.session.get('order_items', [])
    orderItems.append(orderItem.id)
    request.session['order_items'] = orderItems

    discount = event.newStaffDiscount
    if discount:
        request.session["discount"] = discount.codeName

    tokens = TempToken.objects.filter(email=attendee.email)
    for token in tokens:
        token.used = True
        token.save()

    return JsonResponse({'success': True})



###################################
# Staff

def staff(request, guid):
    event = Event.objects.get(default=True)
    context = {'token': guid, 'event': event}
    return render(request, 'registration/staff/staff-locate.html', context)


def staffDone(request):
    event = Event.objects.get(default=True)
    context = {'event': event}
    return render(request, 'registration/staff/staff-done.html', context)


def findStaff(request):
    try:
        postData = json.loads(request.body)
        email = postData['email']
        token = postData['token']

        staff = Staff.objects.get(attendee__email__iexact=email, registrationToken=token)
        if not staff:
            return HttpResponseServerError("No Staff Found")

        request.session['staff_id'] = staff.id
        return JsonResponse({'success': True, 'message':'STAFF'})
    except Exception as e:
        logger.warning("Unable to find staff. " + request.body)
        return HttpResponseServerError(str(e))


def infoStaff(request):
    event = Event.objects.get(default=True)
    context = {'staff': None, 'event': event}
    try:
        staffId = request.session['staff_id']
    except Exception as e:
        return render(request, 'registration/staff-payment.html', context)

    staff = Staff.objects.get(id=staffId)
    if staff:
        staff_dict = model_to_dict(staff)
        attendee_dict = model_to_dict(staff.attendee)
        badges = Badge.objects.filter(attendee=staff.attendee,event=staff.event)

        badge = {}
        if badges.count() > 0:
            badge = badges[0]

        context = {'staff': staff, 'jsonStaff': json.dumps(staff_dict, default=handler),
                   'jsonAttendee': json.dumps(attendee_dict, default=handler),
                   'badge': badge, 'event': event}
    return render(request, 'registration/staff/staff-payment.html', context)


def addStaff(request):
    try:
        postData = json.loads(request.body)
    except ValueError as e:
        logger.error("Unable to decode JSON for addStaff()")
        return JsonResponse({'success': False})

    event = Event.objects.get(default=True)

    #create attendee from request post
    pda = postData['attendee']
    pds = postData['staff']
    pdp = postData['priceLevel']
    evt = postData['event']

    if evt:
      event = Event.objects.get(name=evt)
    else:
      event = Event.objects.get(default=True)

    attendee = Attendee.objects.get(id=pda['id'])
    if not attendee:
        return JsonResponse({'success': False, 'message': 'Attendee not found'})

    tz = timezone.get_current_timezone()
    birthdate = tz.localize(datetime.strptime(pda['birthdate'], '%Y-%m-%d' ))

    attendee.firstName=pda['firstName']
    attendee.lastName=pda['lastName']
    attendee.address1=pda['address1']
    attendee.address2=pda['address2']
    attendee.city=pda['city']
    attendee.state=pda['state']
    attendee.country=pda['country']
    attendee.postalCode=pda['postal']
    attendee.birthdate=birthdate
    attendee.phone=pda['phone']
    attendee.emailsOk=True
    attendee.surveyOk=False  #staff get their own survey

    try:
        attendee.save()
    except Exception as e:
        logger.exception("Error saving staff attendee record.")
        return JsonResponse({'success': False, 'message': 'Attendee not saved: ' + e})

    staff = Staff.objects.get(id=pds['id'])
    if 'staff_id' not in request.session:
        return JsonResponse({'success': False, 'message': 'Staff record not found'})

    ## Update Staff info
    if not staff:
        return JsonResponse({'success': False, 'message': 'Staff record not found'})

    shirt = ShirtSizes.objects.get(id=pds['shirtsize'])
    staff.twitter = pds['twitter']
    staff.telegram = pds['telegram']
    staff.shirtsize = shirt
    staff.specialSkills = pds['specialSkills']
    staff.specialFood = pds['specialFood']
    staff.specialMedical = pds['specialMedical']
    staff.contactName = pds['contactName']
    staff.contactPhone = pds['contactPhone']
    staff.contactRelation = pds['contactRelation']

    try:
        staff.save()
    except Exception as e:
        logger.exception("Error saving staff record.")
        return JsonResponse({'success': False, 'message': 'Staff not saved: ' + str(e)})

    badges = Badge.objects.filter(attendee=attendee,event=event)
    if badges.count() == 0:
        badge = Badge(attendee=attendee,event=event,badgeName=pda['badgeName'])
    else:
        badge = badges[0]
        badge.badgeName = pda['badgeName']

    try:
        badge.save()
    except Exception as e:
        logger.exception("Error saving staff badge record.")
        return JsonResponse({'success': False, 'message': 'Badge not saved: ' + str(e)})

    priceLevel = PriceLevel.objects.get(id=int(pdp['id']))

    orderItem = OrderItem(badge=badge, priceLevel=priceLevel, enteredBy="WEB")
    orderItem.save()

    for option in pdp['options']:
        plOption = PriceLevelOption.objects.get(id=int(option['id']))
        attendeeOption = AttendeeOptions(option=plOption, orderItem=orderItem, optionValue=option['value'])
        attendeeOption.save()

    orderItems = request.session.get('order_items', [])
    orderItems.append(orderItem.id)
    request.session['order_items'] = orderItems

    discount = event.staffDiscount
    if discount:
        request.session["discount"] = discount.codeName

    staff.resetToken()

    return JsonResponse({'success': True})

def checkoutStaff(request):
    sessionItems = request.session.get('order_items', [])
    pdisc = request.session.get('discount', "")
    staffId = request.session['staff_id']
    orderItems = list(OrderItem.objects.filter(id__in=sessionItems))
    try:
        postData = json.loads(request.body)
    except ValueError as e:
        logger.error("Unable to decode JSON for checkoutStaff()")
        return JsonResponse({'success': False})

    discount = Discount.objects.get(codeName="StaffDiscount")
    staff = Staff.objects.get(id=staffId)
    subtotal = getStaffTotal(orderItems, discount, staff)

    if subtotal == 0:
      status, message, order = doZeroCheckout(discount, None, orderItems)
      if not status:
          return JsonResponse({'success': False, 'message': message})

      request.session.flush()
      try:
          sendStaffRegistrationEmail(order.id)
      except Exception as e:
          logger.exception("Error emailing StaffRegistrationEmail - zero sum.")
          staffEmail = getStaffEmail()
          return JsonResponse({'success': False, 'message': "Your registration succeeded but we may have been unable to send you a confirmation email. If you have any questions, please contact {0} to get your confirmation number.".format(staffEmail)})
      return JsonResponse({'success': True})



    pbill = postData["billingData"]
    porg = Decimal(postData["orgDonation"].strip() or '0.00')
    pcharity = Decimal(postData["charityDonation"].strip() or '0.00')
    if porg < 0:
        porg = 0
    if pcharity < 0:
        pcharity = 0

    total = subtotal + porg + pcharity
    ip = get_client_ip(request)

    status, message, order = doCheckout(pbill, total, discount, orderItems, porg, pcharity, ip)

    if status:
        clear_session(request)
        try:
            sendStaffRegistrationEmail(order.id)
        except Exception as e:
            logger.exception("Error emailing StaffRegistrationEmail.")
            staffEmail = getStaffEmail()
            return abort(400, "Your registration succeeded but we may have been unable to send you a confirmation email. If you have any questions, please contact {0} to get your confirmation number.".format(staffEmail))
        return success()
    else:
        order.delete()
        return abort(400, message)



###################################
# Dealers

def dealers(request, guid):
    event = Event.objects.get(default=True)
    context = {'token': guid, 'event': event}
    return render(request, 'registration/dealer/dealer-locate.html', context)

def thanksDealer(request):
    event = Event.objects.get(default=True)
    context = {'event': event}
    return render(request, 'registration/dealer/dealer-thanks.html', context)

def updateDealer(request):
    event = Event.objects.get(default=True)
    context = {'event': event}
    return render(request, 'registration/dealer/dealer-update.html', context)

def doneDealer(request):
    event = Event.objects.get(default=True)
    context = {'event': event}
    return render(request, 'registration/dealer/dealer-done.html', context)

def dealerAsst(request, guid):
    event = Event.objects.get(default=True)
    context = {'token': guid, 'event': event}
    return render(request, 'registration/dealer/dealerasst-locate.html', context)

def doneAsstDealer(request):
    event = Event.objects.get(default=True)
    context = {'event': event}
    return render(request, 'registration/dealer/dealerasst-done.html', context)

def newDealer(request):
    event = Event.objects.get(default=True)
    tz = timezone.get_current_timezone()
    today = tz.localize(datetime.now())
    context = {'event': event}
    if event.dealerRegStart <= today <= event.dealerRegEnd:
        return render(request, 'registration/dealer/dealer-form.html', context)
    return render(request, 'registration/dealer/dealer-closed.html', context)

def infoDealer(request):
    event = Event.objects.get(default=True)
    context = {'dealer': None, 'event':event}
    try:
      dealerId = request.session['dealer_id']
    except Exception as e:
      return render(request, 'registration/dealer/dealer-payment.html', context)

    dealer = Dealer.objects.get(id=dealerId)
    if dealer:
        badge = Badge.objects.filter(attendee=dealer.attendee, event=dealer.event).last()
        dealer_dict = model_to_dict(dealer)
        attendee_dict = model_to_dict(dealer.attendee)
        if badge is not None:
            badge_dict = model_to_dict(badge)
        else:
            badge_dict = {}
        table_dict = model_to_dict(dealer.tableSize)

        context = {'dealer': dealer, 'badge': badge, 'event': dealer.event,
                   'jsonDealer': json.dumps(dealer_dict, default=handler),
                   'jsonTable': json.dumps(table_dict, default=handler),
                   'jsonAttendee': json.dumps(attendee_dict, default=handler),
                   'jsonBadge': json.dumps(badge_dict, default=handler)}
    return render(request, 'registration/dealer/dealer-payment.html', context)

def findDealer(request):
    try:
        postData = json.loads(request.body)
        email = postData['email']
        token = postData['token']

        dealer = Dealer.objects.get(attendee__email__iexact=email, registrationToken=token)
        if not dealer:
            return HttpResponseServerError("No Dealer Found " + email)

        request.session['dealer_id'] = dealer.id
        return JsonResponse({'success': True, 'message':'DEALER'})
    except Exception as e:
        logger.exception("Error finding dealer. " + email)
        return HttpResponseServerError(str(e))

def findAsstDealer(request):
    try:
        postData = json.loads(request.body)
        email = postData['email']
        token = postData['token']

        dealer = Dealer.objects.get(attendee__email__iexact=email, registrationToken=token)
        if not dealer:
            return HttpResponseServerError("No Dealer Found")

        request.session['dealer_id'] = dealer.id
        return JsonResponse({'success': True, 'message':'DEALER'})
    except Exception as e:
        logger.exception("Error finding assistant dealer.")
        return HttpResponseServerError(str(e))


def invoiceDealer(request):
    sessionItems = request.session.get('order_items', [])
    sessionDiscount = request.session.get('discount', "")
    if not sessionItems:
        context = {'orderItems': [], 'total': 0, 'discount': {}}
        request.session.flush()
    else:
        dealerId = request.session.get('dealer_id', -1)
        if dealerId == -1:
            context = {'orderItems': [], 'total': 0, 'discount': {}}
            request.session.flush()
        else:
            dealer = Dealer.objects.get(id=dealerId)
            orderItems = list(OrderItem.objects.filter(id__in=sessionItems))
            discount = Discount.objects.filter(codeName=sessionDiscount).first()
            total = getDealerTotal(orderItems, discount, dealer)
            context = {'orderItems': orderItems, 'total': total, 'discount': discount, 'dealer': dealer}
    event = Event.objects.get(default=True)
    context['event'] = event
    return render(request, 'registration/dealer/dealer-checkout.html', context)


def addAsstDealer(request):
    context = {'attendee': None, 'dealer': None}
    try:
        dealerId = request.session['dealer_id']
    except Exception as e:
        return render(request, 'registration/dealer/dealerasst-add.html', context)

    dealer = Dealer.objects.get(id=dealerId)
    if dealer.attendee:
        assts = list(DealerAsst.objects.filter(dealer=dealer))
        assistants = []
        for dasst in assts:
            assistants.append(model_to_dict(dasst))
        context = {'attendee': dealer.attendee, 'dealer': dealer, 'asstCount': len(assts), 'jsonAssts': json.dumps(assistants, default=handler)}
    event = Event.objects.get(default=True)
    context['event'] = event
    return render(request, 'registration/dealer/dealerasst-add.html', context)

def checkoutAsstDealer(request):
    try:
        postData = json.loads(request.body)
    except ValueError as e:
        logger.error("Unable to decode JSON for checkoutAsstDealer()")
        return JsonResponse({'success': False})
    pbill = postData["billingData"]
    assts = postData['assistants']
    dealerId = request.session['dealer_id']
    dealer = Dealer.objects.get(id=dealerId)
    event = Event.objects.get(default=True)

    badge = Badge.objects.filter(attendee=dealer.attendee, event=dealer.event).last()

    priceLevel = badge.effectiveLevel()
    if priceLevel is None:
        return JsonResponse({'success': False, 'message': "Dealer acocunt has not been paid. Please pay for your table before adding assistants."})

    originalPartnerCount = dealer.getPartnerCount()

    orderItem = OrderItem(badge=badge, priceLevel=priceLevel, enteredBy="WEB")
    orderItem.save()

    #dealer.partners = assts
    for assistant in assts:
        dasst = DealerAsst(dealer=dealer,event=event,name=assistant['name'],email=assistant['email'],license=assistant['license'])
        dasst.save()
    partnerCount = dealer.getPartnerCount()

    # FIXME: remove hardcoded costs
    partners = partnerCount - originalPartnerCount
    total = Decimal(45*partners)
    if pbill['breakfast']:
        total = total + Decimal(60*partners)
    ip = get_client_ip(request)

    status, message, order = doCheckout(pbill, total, None, [], [orderItem], 0, 0, ip)

    if status:
        request.session.flush()
        try:
            sendDealerAsstEmail(dealer.id)
        except Exception as e:
            logger.exception("Error emailing DealerAsstEmail.")
            dealerEmail = getDealerEmail()
            return JsonResponse({'success': False, 'message': "Your payment succeeded but we may have been unable to send you a confirmation email. If you do not receive one within the next hour, please contact {0} to get your confirmation number.".format(dealerEmail)})
        return JsonResponse({'success': True})
    else:
        orderItem.delete()
        for assistant in assts:
            assistant.delete()
        return JsonResponse({'success': False, 'message': message})


def addDealer(request):
    try:
        postData = json.loads(request.body)
    except ValueError as e:
        logger.error("Unable to decode JSON for addStaff()")
        return JsonResponse({'success': False})

    pda = postData['attendee']
    pdd = postData['dealer']
    evt = postData['event']
    pdp = postData['priceLevel']
    event = Event.objects.get(name=evt)

    if 'dealer_id' not in request.session:
        return HttpResponseServerError("Session expired")

    dealer = Dealer.objects.get(id=pdd['id'])

    ## Update Dealer info
    if not dealer:
        return HttpResponseServerError("Dealer id not found")

    dealer.businessName=pdd['businessName']
    dealer.website=pdd['website']
    dealer.logo=pdd['logo']
    dealer.description=pdd['description']
    dealer.license=pdd['license']
    dealer.needPower=pdd['power']
    dealer.needWifi=pdd['wifi']
    dealer.wallSpace=pdd['wall']
    dealer.nearTo=pdd['near']
    dealer.farFrom=pdd['far']
    dealer.reception=pdd['reception']
    dealer.artShow=pdd['artShow']
    dealer.charityRaffle=pdd['charityRaffle']
    dealer.breakfast=pdd['breakfast']
    dealer.willSwitch=pdd['switch']
    dealer.buttonOffer=pdd['buttonOffer']
    dealer.asstBreakfast=pdd['asstbreakfast']
    dealer.event = event

    try:
        dealer.save()
    except Exception as e:
        logger.exception("Error saving dealer record.")
        return HttpResponseServerError(str(e))

    ## Update Attendee info
    attendee = Attendee.objects.get(id=pda['id'])
    if not attendee:
        return HttpResponseServerError("Attendee id not found")

    attendee.firstName=pda['firstName']
    attendee.lastName=pda['lastName']
    attendee.address1=pda['address1']
    attendee.address2=pda['address2']
    attendee.city=pda['city']
    attendee.state=pda['state']
    attendee.country=pda['country']
    attendee.postalCode=pda['postal']
    attendee.phone=pda['phone']

    try:
        attendee.save()
    except Exception as e:
        logger.exception("Error saving dealer attendee record.")
        return HttpResponseServerError(str(e))


    badge = Badge.objects.get(attendee=attendee,event=event)
    badge.badgeName=pda['badgeName']

    try:
        badge.save()
    except Exception as e:
        logger.exception("Error saving dealer badge record.")
        return HttpResponseServerError(str(e))


    priceLevel = PriceLevel.objects.get(id=int(pdp['id']))

    orderItem = OrderItem(badge=badge, priceLevel=priceLevel, enteredBy="WEB")
    orderItem.save()

    for option in pdp['options']:
        plOption = PriceLevelOption.objects.get(id=int(option['id']))
        attendeeOption = AttendeeOptions(option=plOption, orderItem=orderItem, optionValue=option['value'])
        attendeeOption.save()

    orderItems = request.session.get('order_items', [])
    orderItems.append(orderItem.id)
    request.session['order_items'] = orderItems

    return JsonResponse({'success': True})

def checkoutDealer(request):
    try:
        sessionItems = request.session.get('order_items', [])
        pdisc = request.session.get('discount', "")
        orderItems = list(OrderItem.objects.filter(id__in=sessionItems))
        orderItem = orderItems[0]
        if 'dealer_id' not in request.session:
            return HttpResponseServerError("Session expired")

        dealer = Dealer.objects.get(id=request.session.get('dealer_id'))
        try:
            postData = json.loads(request.body)
        except ValueError as e:
            logger.error("Unable to decode JSON for checkoutDealer()")
            return JsonResponse({'success': False})

        discount = Discount.objects.filter(codeName=pdisc).first()
        subtotal = getDealerTotal(orderItems, discount, dealer)

        if subtotal == 0:

            status, message, order = doZeroCheckout(discount, None, orderItems)
            if not status:
              return JsonResponse({'success': False, 'message': message})

            request.session.flush()

            try:
                sendDealerPaymentEmail(dealer, order)
            except Exception as e:
                logger.exception("Error sending DealerPaymentEmail - zero sum.")
                dealerEmail = getDealerEmail()
                return JsonResponse({'success': False, 'message': "Your registration succeeded but we may have been unable to send you a confirmation email. If you have any questions, please contact {0}".format(dealerEmail)})
            return JsonResponse({'success': True})

        porg = Decimal(postData["orgDonation"].strip() or '0.00')
        pcharity = Decimal(postData["charityDonation"].strip() or '0.00')
        if porg < 0:
            porg = 0
        if pcharity < 0:
            pcharity = 0

        total = subtotal + porg + pcharity

        pbill = postData['billingData']
        ip = get_client_ip(request)
        status, message, order = doCheckout(pbill, total, discount, None, orderItems, porg, pcharity, ip)

        if status:
            request.session.flush()
            try:
                dealer.resetToken()
                sendDealerPaymentEmail(dealer, order)
            except Exception as e:
                logger.exception("Error sending DealerPaymentEmail. " + request.body)
                dealerEmail = getDealerEmail()
                return JsonResponse({'success': False, 'message': "Your registration succeeded but we may have been unable to send you a confirmation email. If you have any questions, please contact {0}".format(dealerEmail)})
            return JsonResponse({'success': True})
        else:
            order.delete()
            return JsonResponse({'success': False, 'message': message})
    except Exception as e:
        logger.exception("Error in dealer checkout.")
        return HttpResponseServerError(str(e))

def addNewDealer(request):
    try:
        postData = json.loads(request.body)
    except ValueError as e:
        logger.error("Unable to decode JSON for addNewDealer()")
        return JsonResponse({'success': False})

    try:
        #create attendee from request post
        pda = postData['attendee']
        pdd = postData['dealer']
        evt = postData['event']

        tz = timezone.get_current_timezone()
        birthdate = tz.localize(datetime.strptime(pda['birthdate'], '%Y-%m-%d' ))
        event = Event.objects.get(name=evt)

        attendee = Attendee(firstName=pda['firstName'], lastName=pda['lastName'], address1=pda['address1'], address2=pda['address2'],
                            city=pda['city'], state=pda['state'], country=pda['country'], postalCode=pda['postal'],
                            phone=pda['phone'], email=pda['email'], birthdate=birthdate,
                            emailsOk=bool(pda['emailsOk']), surveyOk=bool(pda['surveyOk'])
                            )
        attendee.save()

        badge = Badge(attendee=attendee, event=event, badgeName=pda['badgeName'])
        badge.save()

        tablesize = TableSize.objects.get(id=pdd['tableSize'])
        dealer = Dealer(attendee=attendee, event=event, businessName=pdd['businessName'], logo=pdd['logo'],
                        website=pdd['website'], description=pdd['description'], license=pdd['license'], needPower=pdd['power'],
                        needWifi=pdd['wifi'], wallSpace=pdd['wall'], nearTo=pdd['near'], farFrom=pdd['far'], tableSize=tablesize,
                        chairs=pdd['chairs'], reception=pdd['reception'], artShow=pdd['artShow'], charityRaffle=pdd['charityRaffle'],
                        breakfast=pdd['breakfast'], willSwitch=pdd['switch'], tables=pdd['tables'],
                        agreeToRules=pdd['agreeToRules'], buttonOffer=pdd['buttonOffer'], asstBreakfast=pdd['asstbreakfast']
                        )
        dealer.save()
    
        partners = pdd['partners']
        for partner in partners:
            dealerPartner = DealerAsst(dealer=dealer, event=event, name=partner['name'],
                                       email=partner['email'], license=partner['license'])
            dealerPartner.save()

        try:
            sendDealerApplicationEmail(dealer.id)
        except Exception as e:
            logger.exception("Error sending DealerApplicationEmail.")
            dealerEmail = getDealerEmail()
            return JsonResponse({'success': False, 'message': "Your registration succeeded but we may have been unable to send you a confirmation email. If you have any questions, please contact {0}.".format(dealerEmail)})
        return JsonResponse({'success': True})

    except Exception as e:
        logger.exception("Error in dealer addition." + request.body)
        return HttpResponseServerError(str(e))

###################################
# Attendees - Onsite

def onsite(request):
    event = Event.objects.get(default=True)
    tz = timezone.get_current_timezone()
    today = tz.localize(datetime.now())
    context = {}
    if event.onlineRegStart <= today <= event.onlineRegEnd:
        return render(request, 'registration/onsite.html', context)
    return render(request, 'registration/closed.html', context)

def onsiteCart(request):
    sessionItems = request.session.get('order_items', [])
    if not sessionItems:
        context = {'orderItems': [], 'total': 0}
        request.session.flush()
    else:
        orderItems = list(OrderItem.objects.filter(id__in=sessionItems))
        total = getTotal([], orderItems)
        context = {'orderItems': orderItems, 'total': total}
    return render(request, 'registration/onsite-checkout.html', context)

def onsiteDone(request):
    context = {}
    request.session.flush()
    return render(request, 'registration/onsite-done.html', context)

@staff_member_required
def onsiteAdmin(request):
    # Modify a dummy session variable to keep it alive
    request.session['heartbeat'] = time.time()

    event = Event.objects.get(default=True)
    terminals = list(Firebase.objects.all())
    term = request.session.get('terminal', None)
    query = request.GET.get('search', None)

    errors = []
    results = None

    # Set default payment terminal to use:
    if term is None and len(terminals) > 0:
        request.session['terminal'] = terminals[0].id

    if len(terminals) == 0:
        errors.append({
            'type' : 'danger',
            'code' : 'ERROR_NO_TERMINAL',
            'text' : 'It looks like no payment terminals have been configured '
            'for this server yet. Check that the APIS Terminal app is '
            'running, and has been configured for the correct URL and API key.'
        })


    # No terminal selection saved in session - see if one's
    # on the URL (that way it'll survive session timeouts)
    url_terminal = request.GET.get('terminal', None)
    logger.info("Terminal from GET parameter: {0}".format(url_terminal))
    if url_terminal is not None:
        try:
            terminal_obj = Firebase.objects.get(id=int(url_terminal))
            request.session['terminal'] = terminal_obj.id
        except Firebase.DoesNotExist:
            errors.append({'type' : 'warning', 'text' : 'The payment terminal specified has not registered with the server'})
        except ValueError:
            # weren't passed an integer
            errors.append({'type' : 'danger', 'text' : 'Invalid terminal specified'})

    if query is not None:
        results = Badge.objects.filter(
            Q(attendee__lastName__icontains=query) | Q(attendee__firstName__icontains=query),
            Q(event=event)
        )
        if len(results) == 0:
            errors.append({'type' : 'warning', 'text' : 'No results for query "{0}"'.format(query)})

    context = {
        'terminals' : terminals,
        'errors' : errors,
        'results' : results,
        'printer_uri' : settings.REGISTER_PRINTER_URI,
    }

    return render(request, 'registration/onsite-admin.html', context)

@staff_member_required
def onsiteAdminSearch(request):
    event = Event.objects.get(default=True)
    terminals = list(Firebase.objects.all())
    query = request.POST.get('search', None)
    if query is None:
        return redirect('onsiteAdmin')

    errors = []
    results = Badge.objects.filter(
        Q(attendee__lastName__icontains=query) | Q(attendee__firstName__icontains=query),
        Q(event=event)
    )
    if len(results) == 0:
        errors = [{'type' : 'warning', 'text' : 'No results for query "{0}"'.format(query)}]

    context = {
        'terminals' : terminals,
        'errors' : errors,
        'results' : results
    }
    return render(request, 'registration/onsite-admin.html', context)

def get_age(obj):
    born = obj.attendee.birthdate
    today = date.today()
    age = today.year - born.year - ((today.month, today.day) < (born.month, born.day))
    return age

@staff_member_required
def onsiteAdminCart(request):
    # Returns dataset to render onsite cart preview
    request.session['heartbeat'] = time.time()  # Keep session alive
    cart = request.session.get('cart', None)
    if cart is None:
        return JsonResponse({'success' : False, 'reason' : 'Cart not initialized'})

    badges = []
    for id in cart:
        try:
            badge = Badge.objects.get(id=id)
            badges.append(badge)
        except Badge.DoesNotExist:
            cart.remove(id)
            logger.error("ID {0} was in cart but doesn't exist in the database".format(id))

    order = None
    subtotal = 0
    result = []
    first_order = None
    for badge in badges:
        oi = badge.getOrderItems()
        level = None
        for item in oi:
            level = item.priceLevel
            # WHY?
            if item.order is not None:
                order = item.order
        if level is None:
            effectiveLevel = None
        else:
            effectiveLevel = {
                'name' : level.name,
                'price' : level.basePrice
            }
            subtotal += level.basePrice

        order = badge.getOrder()
        if first_order is None:
            first_order = order
        else:
            # Reassign order references of items in cart to match first:
            order = badge.getOrder()
            order.reference = first_order.reference
            order.save()

        item = {
            'id' : badge.id,
            'firstName' : badge.attendee.firstName,
            'lastName' : badge.attendee.lastName,
            'badgeName' : badge.badgeName,
            'abandoned' : badge.abandoned,
            'effectiveLevel' : effectiveLevel,
            'discount' : badge.getDiscount(),
            'age' : get_age(badge)
        }
        result.append(item)

    total = subtotal
    charityDonation = '?'
    orgDonation = '?'
    if order is not None:
        total += order.orgDonation + order.charityDonation
        charityDonation = order.charityDonation
        orgDonation = order.orgDonation

    data = {
        'success' : True,
        'result' : result,
        'total' : total,
        'charityDonation' : charityDonation,
        'orgDonation' : orgDonation,
    }

    if order is not None:
        data['order_id'] = order.id
        data['reference'] = order.reference
    else:
        data['order_id'] = None
        data['reference'] = None

    notifyTerminal(request, data)

    return JsonResponse(data)

@staff_member_required
def onsiteAddToCart(request):
    id = request.GET.get('id', None)
    if id is None or id == '':
        return JsonResponse({'success' : False, 'reason' : 'Need ID parameter'}, status=400)

    cart = request.session.get('cart', None)
    if cart is None:
        request.session['cart'] = [id,]
        return JsonResponse({'success' : True, 'cart' : [id]})

    if id in cart:
        return JsonResponse({'success' : True, 'cart' : cart})

    cart.append(id)
    request.session['cart'] = cart

    return JsonResponse({'success' : True, 'cart' : cart})

@staff_member_required
def onsiteRemoveFromCart(request):
    id = request.GET.get('id', None)
    if id is None or id == '':
        return JsonResponse({'success' : False, 'reason' : 'Need ID parameter'}, status=400)

    cart = request.session.get('cart', None)
    if cart is None:
        return JsonResponse({'success' : False, 'reason' : 'Cart is empty'})

    try:
        cart.remove(id)
        request.session['cart'] = cart
    except ValueError:
        return JsonResponse({'success' : False, 'cart' : cart, 'reason' : 'Not in cart'})

    return JsonResponse({'success' : True, 'cart' : cart})

@staff_member_required
def onsiteAdminClearCart(request):
    request.session["cart"] = [];
    sendMessageToTerminal(request, {"command" : "clear"})
    return onsiteAdmin(request)

@staff_member_required
def closeTerminal(request):
    data = { "command" : "close" }
    return sendMessageToTerminal(request, data)

@staff_member_required
def openTerminal(request):
    data = { "command" : "open" }
    return sendMessageToTerminal(request, data)

def sendMessageToTerminal(request, data):
    request.session['heartbeat'] = time.time()  # Keep session alive
    url_terminal = request.GET.get('terminal', None)
    logger.info("Terminal from GET parameter: {0}".format(url_terminal))
    session_terminal = request.session.get('terminal', None)

    if url_terminal is not None:
        try:
            active = Firebase.objects.get(id=int(url_terminal))
            request.session['terminal'] = active.id
        except Firebase.DoesNotExist:
            return JsonResponse({'success' : False, 'message' : 'The payment terminal specified has not registered with the server'}, status=500)
        except ValueError:
            # weren't passed an integer
            return JsonResponse({'success' : False, 'message' : 'Invalid terminal specified'}, status=400)

    try:
        active = Firebase.objects.get(id=session_terminal)
    except Firebase.DoesNotExist:
        return JsonResponse({'success' : False, 'message' : 'No terminal specified and none in session'}, status=400)

    logger.info("Terminal from session: {0}".format(request.session['terminal']))

    to = [active.token,]

    PushyAPI.sendPushNotification(data, to, None)
    return JsonResponse({'success' : True})

@staff_member_required
def enablePayment(request):
    data = { "command" : "enable_payment" }
    return sendMessageToTerminal(request, data)


def notifyTerminal(request, data):
    # Generates preview layout based on cart items and sends the result
    # to the apropriate payment terminal for display to the customer
    term = request.session.get('terminal', None)
    if term is None:
        return
    try:
        active = Firebase.objects.get(id=term)
    except Firebase.DoesNotExist:
        return

    html = render_to_string('registration/customer-display.html', data)
    note = render_to_string('registration/customer-note.txt', data)

    logger.info(note)

    if len(data['result']) == 0:
        display = { "command" : "clear" }
    else:
        display = {
            "command" : "display",
            "html" : html,
            "note" : note,
            "total" : int(data['total'] * 100),
            "reference" : data['reference']
        }

    logger.info(display)

    # Send cloud push message
    logger.debug(note)
    to = [active.token,]

    PushyAPI.sendPushNotification(display, to, None)


@staff_member_required
def onsiteSelectTerminal(request):
    selected = request.POST.get('terminal', None)
    try:
        active = Firebase.objects.get(id=selected)
    except Firebase.DoesNotExist:
        return JsonResponse({'success' : False, 'reason' : 'Terminal does not exist'}, status=404)
    request.session['terminal'] = selected
    return JsonResponse({'success' : True})

#@staff_member_required
def assignBadgeNumber(request):
    badge_id = request.GET.get('id');
    badge_number = request.GET.get('number')
    badge_name = request.GET.get('badge', None)
    badge = None
    event = Event.objects.get(default=True)

    if badge_name is not None:
        try:
            badge = Badge.objects.filter(badgeName__icontains=badge_name, event__name=event.name).first()
        except:
            return JsonResponse({'success' : False, 'reason' : 'Badge name search returned no results'})
    else:
        if badge_id is None or badge_number is None:
            return JsonResponse({'success' : False, 'reason' : 'id and number are required parameters'}, status=400)

    try:
        badge_number = int(badge_number)
    except ValueError:
        return JsonResponse({'success': False, 'message': 'Badge number must be an integer'}, status=400)


    if badge is None:
        try:
            badge = Badge.objects.get(id=int(badge_id))
        except Badge.DoesNotExist:
            return JsonResponse({'success': False, 'message': 'Badge ID specified does not exist'}, status=404)
        except ValueError:
            return JsonResponse({'success': False, 'message': 'Badge ID must be an integer'}, status=400)

    try:
        if badge_number < 0:
            # Auto assign
            badges = Badge.objects.filter(event=badge.event)
            highest = badges.aggregate(Max('badgeNumber'))['badgeNumber__max']
            highest = highest + 1
            badge.badgeNumber = highest
        else:
            badge.badgeNumber = badge_number
        badge.save()
    except Exception as e:
        return JsonResponse({'success' : False, 'message' : 'Error while saving badge number', 'error' : str(e)}, status=500)

    return JsonResponse({'success' : True})

def get_attendee_age(attendee):
    born = attendee.birthdate
    today = date.today()
    age = today.year - born.year - ((today.month, today.day) < (born.month, born.day))
    return age


@staff_member_required
def onsitePrintBadges(request):
    badge_list = request.GET.getlist('id')
    con = printing_main(local=True)
    tags = []
    theme = ""

    for badge_id in badge_list:
        try:
            badge = Badge.objects.get(id=badge_id)
        except Badge.DoesNotExist:
            return JsonResponse({'success' : False, 'message' : 'Badge id {0} does not exist'.format(badge_id)}, status=404)

        theme = badge.event.badgeTheme

        if badge.badgeNumber is None:
            badgeNumber = ''
        else:
            badgeNumber = '{:04}'.format(badge.badgeNumber)

        tags.append({
            'name' : badge.badgeName,
            'number' : badgeNumber,
            'level' : str(badge.effectiveLevel()),
            'title' : '',
            'age'    : get_attendee_age(badge.attendee)
        })
        badge.printed = True
        badge.save()

    if theme == "":
        theme == "apis"
    con.nametags(tags, theme=theme)
    pdf_path = con.pdf.split('/')[-1]

    file_url = reverse(printNametag) + '?file={0}'.format(pdf_path)

    return JsonResponse({
        'success' : True,
        'file' : pdf_path,
        'next' : request.get_full_path(),
        'url' : file_url
    })


#@staff_member_required
def onsiteSignature(request):
    context = {}
    return render(request, 'registration/signature.html', context)

@staff_member_required
@user_passes_test(in_group('Manager'))
def manualDiscount(request):
    # FIXME stub
    raise NotImplementedError


###################################
# Attendees

def checkBanList(firstName, lastName, email):
    banCheck = BanList.objects.filter(firstName=firstName, lastName=lastName, email=email)
    if banCheck.count() > 0:
        return True
    return False

def upgrade(request, guid):
    event = Event.objects.get(default=True)
    context = {'token': guid, 'event': event}
    return render(request, 'registration/attendee-locate.html', context)

def infoUpgrade(request):
    try:
        postData = json.loads(request.body)
    except ValueError as e:
        logger.error("Unable to decode JSON for infoUpgrade()")
        return JsonResponse({'success': False})

    try:
        email = postData['email']
        token = postData['token']

        evt = postData['event']
        event = Event.objects.get(name=evt)

        badge = Badge.objects.get(registrationToken=token)
        if not badge:
          return HttpResponseServerError("No Record Found")

        attendee = badge.attendee
        if attendee.email.lower() != email.lower():
          return HttpResponseServerError("No Record Found")

        request.session['attendee_id'] = attendee.id
        request.session['badge_id'] = badge.id
        return JsonResponse({'success': True, 'message':'ATTENDEE'})
    except Exception as e:
        logger.exception("Error in starting upgrade.")
        return HttpResponseServerError(str(e))

def findUpgrade(request):
    event = Event.objects.get(default=True)
    context = {'attendee': None, 'event': event}
    try:
      attId = request.session['attendee_id']
      badgeId = request.session['badge_id']
    except Exception as e:
      return render(request, 'registration/attendee-upgrade.html', context)

    attendee = Attendee.objects.get(id=attId)
    if attendee:
        badge = Badge.objects.get(id=badgeId)
        attendee_dict = model_to_dict(attendee)
        badge_dict = {'id': badge.id}
        lvl = badge.effectiveLevel()
        existingOIs = badge.getOrderItems()
        lvl_dict = {'basePrice': lvl.basePrice, 'options': getOptionsDict(existingOIs)}
        context = {'attendee': attendee, 
                   'badge': badge,
                   'event': event,
                   'jsonAttendee': json.dumps(attendee_dict, default=handler),
                   'jsonBadge': json.dumps(badge_dict, default=handler),
                   'jsonLevel': json.dumps(lvl_dict, default=handler)}
    return render(request, 'registration/attendee-upgrade.html', context)

def addUpgrade(request):
    try:
        postData = json.loads(request.body)
    except ValueError as e:
        logger.error("Unable to decode JSON for addUpgrade()")
        return JsonResponse({'success': False})

    pda = postData['attendee']
    pdp = postData['priceLevel']
    pdd = postData['badge']
    evt = postData['event']
    event = Event.objects.get(name=evt)

    if 'attendee_id' not in request.session:
        return HttpResponseServerError("Session expired")

    ## Update Attendee info
    attendee = Attendee.objects.get(id=pda['id'])
    if not attendee:
        return HttpResponseServerError("Attendee id not found")

    badge = Badge.objects.get(id=pdd['id'])
    priceLevel = PriceLevel.objects.get(id=int(pdp['id']))

    orderItem = OrderItem(badge=badge, priceLevel=priceLevel, enteredBy="WEB")
    orderItem.save()

    for option in pdp['options']:
        plOption = PriceLevelOption.objects.get(id=int(option['id']))
        attendeeOption = AttendeeOptions(option=plOption, orderItem=orderItem, optionValue=option['value'])
        attendeeOption.save()

    orderItems = request.session.get('order_items', [])
    orderItems.append(orderItem.id)
    request.session['order_items'] = orderItems

    return JsonResponse({'success': True})

def invoiceUpgrade(request):
    sessionItems = request.session.get('order_items', [])
    if not sessionItems:
        context = {'orderItems': [], 'total': 0, 'discount': {}}
        request.session.flush()
    else:
        attendeeId = request.session.get('attendee_id', -1)
        badgeId = request.session.get('badge_id', -1)
        if attendeeId == -1 or badgeId == -1:
            context = {'orderItems': [], 'total': 0, 'discount': {}}
            request.session.flush()
        else:
            badge = Badge.objects.get(id=badgeId)
            attendee = Attendee.objects.get(id=attendeeId)
            lvl = badge.effectiveLevel()
            lvl_dict = {'basePrice': lvl.basePrice}
            orderItems = list(OrderItem.objects.filter(id__in=sessionItems))
            total = getTotal([], orderItems)
            context = {
                'orderItems': orderItems, 
                'total': total,
                'attendee': attendee,
                'prevLevel': lvl_dict,
                'event': badge.event,
            }
    return render(request, 'registration/upgrade-checkout.html', context)

def doneUpgrade(request):
    event = Event.objects.get(default=True)
    context = { 'event' : event }
    return render(request, 'registration/upgrade-done.html', context)

def checkoutUpgrade(request):
  try:
    sessionItems = request.session.get('order_items', [])
    orderItems = list(OrderItem.objects.filter(id__in=sessionItems))
    if 'attendee_id' not in request.session:
        return HttpResponseServerError("Session expired")

    attendee = Attendee.objects.get(id=request.session.get('attendee_id'))
    try:
        postData = json.loads(request.body)
    except ValueError as e:
        logger.error("Unable to decode JSON for checkoutUpgrade()")
        return JsonResponse({'success': False})

    event = Event.objects.get(default=True)

    subtotal = getTotal([], orderItems)

    if subtotal == 0:
        status, message, order = doZeroCheckout(None, None, orderItems)

        if not status:
            return JsonResponse({'success': False, 'message': message})

        request.session.flush()
        try:
            sendUpgradePaymentEmail(attendee, order)
        except Exception as e:
            logger.exception("Error sending UpgradePaymentEmail - zero sum.")
            registrationEmail = getRegistrationEmail(event)
            return JsonResponse({'success': False, 'message': "Your upgrade payment succeeded but we may have been unable to send you a confirmation email. If you do not receive one within the next hour, please contact {0} to get your confirmation number.".format(registrationEmail)})
        return JsonResponse({'success': True})

    porg = Decimal(postData["orgDonation"].strip() or '0.00')
    pcharity = Decimal(postData["charityDonation"].strip() or '0.00')
    if porg < 0:
        porg = 0
    if pcharity < 0:
        pcharity = 0

    total = subtotal + porg + pcharity

    pbill = postData['billingData']
    ip = get_client_ip(request)
    status, message, order = doCheckout(pbill, total, None, [], orderItems, porg, pcharity, ip)

    if status:
        request.session.flush()
        try:
            sendUpgradePaymentEmail(attendee, order)
        except Exception as e:
            logger.exception("Error sending UpgradePaymentEmail.")
            registrationEmail = getRegistrationEmail(event)
            return JsonResponse({'success': False, 'message': "Your upgrade payment succeeded but we may have been unable to send you a confirmation email. If you do not receive one within the next hour, please contact {0} to get your confirmation number.".format(registrationEmail)})
        return JsonResponse({'success': True})
    else:
        order.delete()
        return JsonResponse({'success': False, 'message': response})

  except Exception as e:
    logger.exception("Error in attendee upgrade.")
    return HttpResponseServerError(str(e))



def getCart(request):
    sessionItems = request.session.get('cart_items', [])
    sessionOrderItems = request.session.get('order_items', [])
    discount = request.session.get('discount', "")
    event = None
    if not sessionItems and not sessionOrderItems:
        context = {'orderItems': [], 'total': 0, 'discount': {}}
        request.session.flush()
    elif sessionOrderItems:
        orderItems = list(OrderItem.objects.filter(id__in=sessionOrderItems))
        if discount:
            discount = Discount.objects.filter(codeName=discount)
            if discount.count() > 0: discount = discount.first()
        total, total_discount = getTotal([], orderItems, discount)

        hasMinors = False
        for item in orderItems:
            if item.badge.isMinor():
              hasMinors = True
              break

        event = Event.objects.get(default=True)
        context = {
            'event' : event,
            'orderItems': orderItems,
            'total': total,
            'total_discount' : total_discount,
            'discount': discount,
            'hasMinors': hasMinors
        }

    elif sessionItems:
        cartItems = list(Cart.objects.filter(id__in=sessionItems))
        orderItems = []
        if discount:
            discount = Discount.objects.filter(codeName=discount)
            if discount.count() > 0: discount = discount.first()
        total, total_discount = getTotal(cartItems, [], discount)

        hasMinors = False
        for cart in cartItems:
            cartJson = json.loads(cart.formData)
            pda = cartJson['attendee']
            event = Event.objects.get(name=cartJson['event'])
            evt = event.eventStart
            tz = timezone.get_current_timezone()
            birthdate = tz.localize(datetime.strptime(pda['birthdate'], '%Y-%m-%d' ))
            age_at_event = evt.year - birthdate.year - ((evt.month, evt.day) < (birthdate.month, birthdate.day))
            if age_at_event < 18:
              hasMinors = True

            pdp = cartJson['priceLevel']
            priceLevel = PriceLevel.objects.get(id=pdp['id'])
            pdo = pdp['options']
            options = []
            for option in pdo:
                dataOption = {}
                optionData = PriceLevelOption.objects.get(id=option['id'])
                if optionData.optionExtraType == 'int':
                    if option['value']:
                        itemTotal = (optionData.optionPrice*Decimal(option['value']))
                        dataOption = {'name': optionData.optionName, 'number': option['value'], 'total': itemTotal}
                else:
                    itemTotal = optionData.optionPrice
                    dataOption = {'name': optionData.optionName, 'total': itemTotal}
                options.append(dataOption)
            orderItem = {
                'id' : cart.id,
                'attendee': pda,
                'priceLevel': priceLevel,
                'options': options
            }
            orderItems.append(orderItem)

        if event is None:
            event = Event.objects.get(default=True)
        context = {
            'event' : event,
            'orderItems': orderItems,
            'total': total,
            'total_discount' : total_discount,
            'discount': discount,
            'hasMinors': hasMinors
        }
    return render(request, 'registration/checkout.html', context)

def saveCart(cart):
    postData = json.loads(cart.formData)
    pda = postData['attendee']
    pdp = postData['priceLevel']
    evt = postData['event']

    tz = timezone.get_current_timezone()
    birthdate = tz.localize(datetime.strptime(pda['birthdate'], '%Y-%m-%d' ))

    event = Event.objects.get(name=evt)

    attendee = Attendee(firstName=pda['firstName'], lastName=pda['lastName'], 
                        phone=pda['phone'], email=pda['email'], birthdate=birthdate,
                        emailsOk=bool(pda['emailsOk']), volunteerContact=len(pda['volDepts']) > 0, volunteerDepts=pda['volDepts'],
                        surveyOk=bool(pda['surveyOk']), aslRequest=bool(pda['asl']))
    
    if event.collectAddress:
        try:
            attendee.address1=pda['address1']
            attendee.address2=pda['address2']
            attendee.city=pda['city']
            attendee.state=pda['state']
            attendee.country=pda['country']
            attendee.postalCode=pda['postal']
        except KeyError:
            logging.error("Supposed to be collecting addresses, but wasn't provided by form!")
    attendee.save()

    badge = Badge(badgeName=pda['badgeName'], event=event, attendee=attendee)
    badge.save()

    priceLevel = PriceLevel.objects.get(id=int(pdp['id']))

    via = "WEB"
    if postData['attendee'].get('onsite', False):
        via = "ONSITE"

    orderItem = OrderItem(badge=badge, priceLevel=priceLevel, enteredBy=via)
    orderItem.save()

    for option in pdp['options']:
        plOption = PriceLevelOption.objects.get(id=int(option['id']))
        if plOption.optionExtraType == 'int' and option['value'] == '':
            attendeeOption = AttendeeOptions(option=plOption, orderItem=orderItem, optionValue='0')
        else:
            if option['value'] != '':
                attendeeOption = AttendeeOptions(option=plOption, orderItem=orderItem, optionValue=option['value'])
        attendeeOption.save()

    cart.transferedDate = datetime.now()
    cart.save()

    return orderItem

def addToCart(request):
    """
    Create attendee from request post.
    """
    try:
        postData = json.loads(request.body)
    except ValueError as e:
        return abort(400, "Unable to decode JSON body")

    event = Event.objects.get(default=True)

    try:
        pda = postData['attendee']
        pda['firstName']
        pda['lastName']
        pda['email']
    except KeyError:
        return abort(400, "Required parameters not found in POST body")

    banCheck = checkBanList(pda['firstName'], pda['lastName'], pda['email'])
    if banCheck:
        logger.error("***ban list registration attempt***")
        registrationEmail = getRegistrationEmail()
        return abort(403, "We are sorry, but you are unable to register for {0}. If you have any questions, or would like further information or assistance, please contact Registration at {1}".format(event, registrationEmail))

    cart = Cart(form=Cart.ATTENDEE, formData=request.body, formHeaders=getRequestMeta(request))
    cart.save()

    #add attendee to session order
    cartItems = request.session.get('cart_items', [])
    cartItems.append(cart.id)
    request.session['cart_items'] = cartItems
    return success()

def removeFromCart(request):
    #locate attendee in session order
    deleted = False
    order = request.session.get('order_items', [])
    try:
        postData = json.loads(request.body)
    except ValueError as e:
        return abort(400, 'Unable to decode JSON parameters')
    if 'id' not in postData.keys():
        return abort(400, 'Required parameter `id` not specified')
    id = postData['id']

    # Old workflow
    logger.debug("order_items: {0}".format(order))
    logger.debug("delete order from session: {0}".format(id))
    if int(id) in order:
        order.remove(int(id))
        deleted = True
        request.session['order_items'] = order
        return success()

    # New cart workflow
    cartItems = request.session.get('cart_items', [])
    logger.debug("cartItems: {0}".format(cartItems))
    for item in cartItems:
        if str(item) == str(id):
            cart = Cart.objects.get(id=id)
            cart.delete()
            deleted = True
    if not deleted:
        return abort(404, 'Cart ID not in session')
    return success()

def cancelOrder(request):
    # (DEPRECATED) remove order from session
    order = request.session.get('order_items', [])
    for item in order:
        deleteOrderItem(item)
    # Delete carts
    sessionItems = request.session.get('cart_items', [])
    cartItems = Cart.objects.filter(id__in=sessionItems)
    cartItems.delete()
    # Clear session values
    clear_session(request)
    return success() 

def clear_session(request):
    """
    Soft-clears session by removing any non-protected session values.
    (anything prefixed with '_'; keeps Django user logged-in)
    """
    for key in request.session.keys():
        if key[0] != '_':
            del request.session[key]

def checkout(request):
    event = Event.objects.get(default=True)
    sessionItems = request.session.get('cart_items', [])
    cartItems = list(Cart.objects.filter(id__in=sessionItems))
    orderItems = request.session.get('order_items', [])
    pdisc = request.session.get('discount', "")

    # Safety valve (in case session times out before checkout is complete)
    if len(sessionItems) == 0 and len(orderItems) == 0:
        abort(400, "Session expired or no session is stored for this client")

    try:
        postData = json.loads(request.body)
    except ValueError as e:
        logger.error("Unable to decode JSON for checkout()")
        return abort(400, 'Unable to parse input options')

    discount = Discount.objects.filter(codeName=pdisc)
    if discount.count() > 0 and discount.first().isValid():
        discount = discount.first()
    else:
        discount = None

    if orderItems:
        orderItems = list(OrderItem.objects.filter(id__in=orderItems))

    subtotal, _ = getTotal(cartItems, orderItems, discount)

    if subtotal == 0:
        status, message, order = doZeroCheckout(discount, cartItems, orderItems)
        if not status:
            return abort(400, message)

        request.session.flush()
        try:
            sendRegistrationEmail(order, order.billingEmail)
        except Exception as e:
            logger.error("Error sending RegistrationEmail - zero sum.")
            logger.exception(e)
            registrationEmail = getRegistrationEmail(event)
            return abort(400, "Your payment succeeded but we may have been unable to send you a confirmation email. If you do not receive one within the next hour, please contact {0} to get your confirmation number.".format(registrationEmail))
        return success()

    porg = Decimal(postData["orgDonation"].strip() or '0.00')
    pcharity = Decimal(postData["charityDonation"].strip() or '0.00')
    pbill = postData["billingData"]

    if porg < 0:
        porg = 0
    if pcharity < 0:
        pcharity = 0

    total = subtotal + porg + pcharity
    ip = get_client_ip(request)

    onsite = postData["onsite"]
    if onsite:
        att = orderItems[0].badge.attendee
        billingData = {
            'cc_firstname' : att.firstName,
            'cc_lastname' : att.lastName,
            'email' : att.email,
            'address1' : att.address1,
            'address2' : att.address2,
            'city' : att.city,
            'state' : att.state,
            'country' : att.country,
            'postal' : att.postalCode
        }
        reference = getConfirmationToken()
        while Order.objects.filter(reference=reference).count() > 0:
            reference = getConfirmationToken()

        order = Order(total=Decimal(total), reference=reference, discount=discount,
                      orgDonation=porg, charityDonation=pcharity, billingType=Order.UNPAID,
                      billingName=billingData['cc_firstname'] + " " + billingData['cc_lastname'],
                      billingAddress1=billingData['address1'], billingAddress2=billingData['address2'],
                      billingCity=billingData['city'], billingState=billingData['state'], billingCountry=billingData['country'],
                      billingPostal=billingData['postal'], billingEmail=billingData['email'])
        order.status = "Onsite Pending"
        order.save()

        for oitem in orderItems:
            oitem.order = order
            oitem.save()
        if discount:
            discount.used = discount.used + 1
            discount.save()

        status = True
        message = "Onsite success"
    else:
        status, message, order = doCheckout(pbill, total, discount, cartItems, orderItems, porg, pcharity, ip)

    if status:
        # Delete cart when done
        cartItems = Cart.objects.filter(id__in=sessionItems)
        cartItems.delete()
        clear_session(request)
        try:
            sendRegistrationEmail(order, order.billingEmail)
        except Exception as e:
            event = Event.objects.get(default=True)
            registrationEmail = getRegistrationEmail(event)

            logger.error("Error sending RegistrationEmail.")
            logger.exception(e)
            return abort(500, "Your payment succeeded but we may have been unable to send you a confirmation email. If you do not receive one within the next hour, please contact {0} to get your confirmation number.".format(registrationEmail))
        return success()
    else:
        return abort(400, message)


def cartDone(request):
    event = Event.objects.get(default=True)
    context = {'event': event}
    return render(request, 'registration/done.html', context)

###################################
# Staff only access

@staff_member_required
def basicBadges(request):
    badges = Badge.objects.all()
    staff = Staff.objects.all()
    event = Event.objects.get(default=True)

    bdata = [{'badgeName': badge.badgeName, 'level': badge.effectiveLevel().name, 'assoc':badge.abandoned,
              'firstName': badge.attendee.firstName.lower(), 'lastName': badge.attendee.lastName.lower(),
              'printed': badge.printed, 'discount': badge.getDiscount(),
              'orderItems': getOptionsDict(badge.orderitem_set.all()) }
             for badge in badges if badge.effectiveLevel() != None and badge.event == event]

    staffdata = [{'firstName': s.attendee.firstName.lower(), 'lastName':s.attendee.lastName.lower(),
                  'title': s.title, 'id': s.id}
                for s in staff if s.event == event]

    for staff in staffdata:
        sbadge = Staff.objects.get(id=staff['id']).getBadge()
        if sbadge:
            staff['badgeName'] = sbadge.badgeName
            if sbadge.effectiveLevel():
                staff['level'] = sbadge.effectiveLevel().name
            else:
                staff['level'] = "none"
            staff['assoc'] = sbadge.abandoned
            staff['orderItems'] = getOptionsDict(sbadge.orderitem_set.all())

    sdata = sorted(bdata, key=lambda x:(x['level'],x['lastName']))
    ssdata = sorted(staffdata, key=lambda x:x['lastName'])

    dealers = [att for att in sdata if att['assoc'] == 'Dealer']
    staff = [att for att in ssdata]
    attendees = [att for att in sdata if att['assoc'] != 'Staff' ]
    return render(request, 'registration/utility/badgelist.html', {'attendees': attendees, 'dealers': dealers, 'staff': staff})

@staff_member_required
def vipBadges(request):
    badges = Badge.objects.all()
    # Assumes VIP levels based on being marked as "vip" group, or EmailVIP set
    priceLevels = PriceLevel.objects.filter(Q(emailVIP=True) | Q(group__iexact='vip'))
    vipLevels = [ level.name for level in priceLevels ]

    # FIXME list comprehension is sloooooooow - there must be a way to do this in SQL -R
    bdata = [{'badge': badge, 'orderItems': getOptionsDict(badge.orderitem_set.all()),
              'level': badge.effectiveLevel().name, 'assoc': badge.abandoned}
             for badge in badges if badge.effectiveLevel() != None and badge.effectiveLevel() != 'Unpaid' and
               badge.effectiveLevel().name in vipLevels and badge.abandoned != 'Staff']

    events = Event.objects.all()
    # FIXME this doesn't actually affect the order in the resulting template render
    events.reverse()

    return render(request, 'registration/utility/holidaylist.html', {'badges': bdata, 'events': events})



###################################
# Printing

def printNametag(request):
    context = {
        'file' : request.GET.get('file', None),
        'next' : request.GET.get('next', None)
    }
    return render(request, 'registration/printing.html', context)

def servePDF(request):
    filename = request.GET.get('file', None)
    if filename is None:
        return JsonResponse({'success': False})
    filename = filename.replace('..', '/')
    try:
        fsock = open('/tmp/%s' % filename)
    except IOError as e:
        return JsonResponse({'success': False})
    response = HttpResponse(fsock, content_type='application/pdf')
    #response['Content-Disposition'] = 'attachment; filename=download.pdf'
    fsock.close()
    os.unlink('/tmp/%s' % filename)
    return response


###################################
# Utilities

def get_client_ip(request):
    x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
    if x_forwarded_for:
        ip = x_forwarded_for.split(',')[0]
    else:
        ip = request.META.get('REMOTE_ADDR')
    return ip

def getRequestMeta(request):
    values = {}
    values['HTTP_REFERER'] = request.META.get('HTTP_REFERER')
    values['HTTP_USER_AGENT'] = request.META.get('HTTP_USER_AGENT')
    values['IP'] = get_client_ip(request)
    return json.dumps(values)

def getOptionsDict(orderItems):
    orderDict = []
    for oi in orderItems:
        aos = oi.getOptions()
        for ao in aos:
            if ao.optionValue == 0 or ao.optionValue == None or ao.optionValue == "" or ao.optionValue == False: pass
            try:
                orderDict.append({'name': ao.option.optionName, 'value': ao.optionValue, 'id': ao.option.id, 'image': ao.option.optionImage.url})
            except:
                orderDict.append({'name': ao.option.optionName, 'value': ao.optionValue, 'id': ao.option.id, 'image': None})

            orderDict.append({'name': ao.option.optionName, 'value': ao.optionValue,
                              'id': ao.option.id, 'type': ao.option.optionExtraType})
    return orderDict

def getEvents(request):
    events = Event.objects.all()
    data = [{'name': ev.name, 'id': ev.id, 'dealerStart': ev.dealerRegStart, 'dealerEnd': ev.dealerRegEnd, 'staffStart': ev.staffRegStart, 'staffEnd': ev.staffRegEnd, 'attendeeStart': ev.attendeeRegStart, 'attendeeEnd': ev.attendeeRegEnd} for ev in events]
    return HttpResponse(json.dumps(data, cls=DjangoJSONEncoder), content_type='application/json')

def getDepartments(request):
    depts = Department.objects.filter(volunteerListOk=True).order_by('name')
    data = [{'name': item.name, 'id': item.id} for item in depts]
    return HttpResponse(json.dumps(data), content_type='application/json')

def getAllDepartments(request):
    depts = Department.objects.order_by('name')
    data = [{'name': item.name, 'id': item.id} for item in depts]
    return HttpResponse(json.dumps(data), content_type='application/json')

def getPriceLevelList(levels):
    data = [ {
        'name': level.name,
        'id':level.id,
        'base_price': level.basePrice.__str__(),
        'description': level.description,
        'options': [{
            'name': option.optionName,
            'value': option.optionPrice,
            'id': option.id,
            'required': option.required,
            'active': option.active,
            'type': option.optionExtraType,
            'image': option.getOptionImage(),
            'description': option.description,
            'list': option.getList()
            } for option in level.priceLevelOptions.order_by('rank', 'optionPrice').all() ]
          } for level in levels ]
    return data

def getMinorPriceLevels(request):
    now = timezone.now()
    levels = PriceLevel.objects.filter(public=False, startDate__lte=now, endDate__gte=now, name__icontains='minor').order_by('basePrice')
    data = getPriceLevelList(levels)
    return HttpResponse(json.dumps(data, cls=DjangoJSONEncoder), content_type='application/json')

def getAccompaniedPriceLevels(request):
    now = timezone.now()
    levels = PriceLevel.objects.filter(public=False, startDate__lte=now, endDate__gte=now, name__icontains='accompanied').order_by('basePrice')
    data = getPriceLevelList(levels)
    return HttpResponse(json.dumps(data, cls=DjangoJSONEncoder), content_type='application/json')

def getFreePriceLevels(request):
    now = timezone.now()
    levels = PriceLevel.objects.filter(public=False, startDate__lte=now, endDate__gte=now, name__icontains='free')
    data = getPriceLevelList(levels)
    return HttpResponse(json.dumps(data, cls=DjangoJSONEncoder), content_type='application/json')


def getPriceLevels(request):
    dealer = request.session.get('dealer_id', -1)
    staff = request.session.get('staff_id', -1)
    attendee = request.session.get('attendee_id', -1)
    #hide any irrelevant price levels if something in session
    att = None
    if dealer > 0:
        deal = Dealer.objects.get(id=dealer)
        att = deal.attendee
        event = deal.event
        badge = Badge.objects.filter(attendee=att,event=event).last()
    if staff > 0:
        sta = Staff.objects.get(id=staff)
        att = sta.attendee
        event = sta.event
        badge = Badge.objects.filter(attendee=att,event=event).last()
    if attendee > 0:
        att = Attendee.objects.get(id=attendee)
        badge = Badge.objects.filter(attendee=att).last()

    now = timezone.now()
    levels = PriceLevel.objects.filter(public=True, startDate__lte=now, endDate__gte=now).order_by('basePrice')
    if att and badge and badge.effectiveLevel():
        levels = levels.exclude(basePrice__lt=badge.effectiveLevel().basePrice)
    data = getPriceLevelList(levels)
    return HttpResponse(json.dumps(data, cls=DjangoJSONEncoder), content_type='application/json')

def getAdultPriceLevels(request):
    dealer = request.session.get('dealer_id', -1)
    staff = request.session.get('staff_id', -1)
    attendee = request.session.get('attendee_id', -1)
    #hide any irrelevant price levels if something in session
    att = None
    if dealer > 0:
        deal = Dealer.objects.get(id=dealer)
        att = deal.attendee
        event = deal.event
        badge = Badge.objects.filter(attendee=att,event=event).last()
    if staff > 0:
        sta = Staff.objects.get(id=staff)
        att = sta.attendee
        event = sta.event
        badge = Badge.objects.filter(attendee=att,event=event).last()
    if attendee > 0:
        att = Attendee.objects.get(id=attendee)
        badge = Badge.objects.filter(attendee=att).last()

    now = timezone.now()
    levels = PriceLevel.objects.filter(public=True, isMinor=False, startDate__lte=now, endDate__gte=now).order_by('basePrice')
    if att and badge and badge.effectiveLevel():
        levels = levels.exclude(basePrice__lt=badge.effectiveLevel().basePrice)
    data = getPriceLevelList(levels)
    return HttpResponse(json.dumps(data, cls=DjangoJSONEncoder), content_type='application/json')

def getShirtSizes(request):
    sizes = ShirtSizes.objects.all()
    data = [{'name': size.name, 'id': size.id} for size in sizes]
    return HttpResponse(json.dumps(data), content_type='application/json')

def getTableSizes(request):
    event = Event.objects.get(default=True)
    sizes = TableSize.objects.filter(event=event)
    data = [{'name': size.name, 'id': size.id, 'description': size.description, 'chairMin': size.chairMin, 'chairMax': size.chairMax, 'tableMin': size.tableMin, 'tableMax': size.tableMax, 'partnerMin': size.partnerMin, 'partnerMax': size.partnerMax, 'basePrice': str(size.basePrice)} for size in sizes]
    return HttpResponse(json.dumps(data), content_type='application/json')

def getSessionAddresses(request):
    event = Event.objects.get(default=True)
    sessionItems = request.session.get('cart_items', [])
    if not sessionItems:
        #might be from dealer workflow, which is order items in the session
        sessionItems = request.session.get('order_items', [])
        if not sessionItems:
            data = {}
        else:
            orderItems = OrderItem.objects.filter(id__in=sessionItems)
            data = [{'id': oi.badge.attendee.id, 'fname': oi.badge.attendee.firstName,
                 'lname': oi.badge.attendee.lastName, 'email': oi.badge.attendee.email,
                 'address1': oi.badge.attendee.address1, 'address2': oi.badge.attendee.address2,
                 'city': oi.badge.attendee.city, 'state': oi.badge.attendee.state, 'country': oi.badge.attendee.country,
                 'postalCode': oi.badge.attendee.postalCode} for oi in orderItems]
    else:
        data = []
        cartItems = list(Cart.objects.filter(id__in=sessionItems))
        for cart in cartItems:
            cartJson = json.loads(cart.formData)
            pda = cartJson['attendee']
            cartItem = {
                'fname': pda['firstName'],
                'lname': pda['lastName'],
                'email': pda['email'],
                'phone': pda['phone'],
            }
            if event.collectAddress:
                cartItem.update({
                    'address1': pda['address1'],
                    'address2': pda['address2'],
                    'city': pda['city'], 
                    'state': pda['state'],
                    'postalCode': pda['postal'],
                    'country': pda['country']
                })

            data.append(cartItem)
    return HttpResponse(json.dumps(data), content_type='application/json')

@csrf_exempt
def completeSquareTransaction(request):
    key = request.GET.get('key', '')
    reference = request.GET.get('reference', None)
    clientTransactionId = request.GET.get('clientTransactionId', None)
    serverTransactionId = request.GET.get('serverTransactionId', None)

    if key != settings.REGISTER_KEY:
        return JsonResponse({'success' : False, 'reason' : 'Incorrect API key'}, status=401)

    if reference is None or clientTransactionId is None:
        return JsonResponse({'success' : False, 'reason' : 'Reference and clientTransactionId are required parameters'}, status=400)

    # Things we need:
    #   orderID or reference (passed to square by metadata)
    # Square returns:
    #   clientTransactionId (offline payments)
    #   serverTransactionId (online payments)

    try:
        #order = Order.objects.get(reference=reference)
        orders = Order.objects.filter(reference=reference)
    except Order.DoesNotExist:
        return JsonResponse({'success' : False, 'reason' : 'No order matching the reference specified exists'}, status=404)

    for order in orders:
        order.billingType = Order.CREDIT
        order.status = "Complete"
        order.settledDate = datetime.now()
        order.notes = json.dumps({
            'type' : 'square',
            'clientTransactionId' : clientTransactionId,
            'serverTransactionId' : serverTransactionId
        })
        order.save()

    return JsonResponse({'success' : True})

def completeCashTransaction(request):
    reference = request.GET.get('reference', None)
    total = request.GET.get('total', None)
    tendered = request.GET.get('tendered', None)

    if reference is None or tendered is None or total is None:
        return JsonResponse({'success' : False, 'reason' : 'Reference, tendered, and total are required parameters'}, status=400)

    try:
        order = Order.objects.get(reference=reference)
    except Order.DoesNotExist:
        return JsonResponse({'success' : False, 'reason' : 'No order matching the reference specified exists'}, status=404)

    order.billingType = Order.CASH
    order.status = "Complete"
    order.settledDate = datetime.now()
    order.notes = json.dumps({
        'type'     : 'cash',
        'tendered' : tendered
    })
    order.save()

    txn = Cashdrawer(action=Cashdrawer.TRANSACTION, total=total, tendered=tendered, user=request.user)
    txn.save()

    return JsonResponse({'success' : True})


@csrf_exempt
def firebaseRegister(request):
    key = request.GET.get('key', '')
    if key != settings.REGISTER_KEY:
        return JsonResponse({'success' : False, 'reason' : 'Incorrect API key'}, status=401)

    token = request.GET.get('token', None)
    name = request.GET.get('name', None)
    if token is None or name is None:
        return JsonResponse({'success' : False, 'reason' : 'Must specify token and name parameter'}, status=400)

    # Upsert if a new token with an existing name tries to register
    try:
        old_terminal = Firebase.objects.get(name=name)
        old_terminal.token = token
        old_terminal.save()
        return JsonResponse({'success' : True, 'updated' : True})
    except Firebase.DoesNotExist:
        pass
    except Exception as e:
        return JsonResponse({'success' : False, 'reason' : 'Failed while attempting to update existing name entry'}, status=500)

    try:
        terminal = Firebase(token=token, name=name)
        terminal.save()
    except Exception as e:
        logger.exception(e)
        logger.error("Error while saving Firebase token to database")
        return JsonResponse({'success' : False, 'reason' : 'Error while saving to database'}, status=500)

    return JsonResponse({'success' : True, 'updated' : False})

@csrf_exempt
def firebaseLookup(request):
    # Returns the common name stored for a given firebase token
    # (So client can notify server if either changes)
    token = request.GET.get('token', None)
    if token is None:
        return JsonResponse({'success' : False, 'reason' : 'Must specify token parameter'}, status=400)

    try:
        terminal = Firebase.objects.get(token=token)
        return JsonResponse({'success' : True, 'name' : terminal.name, 'closed' : terminal.closed})
    except Firebase.DoesNotExist:
        return JsonResponse({'success' : False, 'reason' : 'No such token registered'}, status=404)



##################################
# Not Endpoints

def abort(status=400, reason="Bad request"):
    """
    Returns a JSON response indicating an error to the client.

    status: A valid HTTP status code
    reason: Human-readable explanation
    """
    logger.error("JSON {0}: {1}".format(status, reason))
    return JsonResponse({
        'success': False,
        'reason' : reason
    }, status=status)

def success(status=200, reason=None):
    """
    Returns a JSON response indicating success.

    status: A valid HTTP status code (2xx)
    reason: (Optional) human-readable explanation
    """
    if reason is None:
        logger.debug("JSON {0}".format(status))
        return JsonResponse({'success': True}, status=status)
    else:
        logger.debug("JSON {0}: {1}".format(status, reason))
        return JsonResponse({
            'success': True,
            'reason': reason,
            'message': reason   #Backwards compatibility
        }, status=status)

def getConfirmationToken():
    return ''.join(random.SystemRandom().choice(string.ascii_uppercase+string.digits) for _ in range(6))

def deleteOrderItem(id):
    orderItems = OrderItem.objects.filter(id=id)
    if orderItems.count() == 0:
      return
    orderItem = orderItems.first()
    orderItem.badge.attendee.delete()
    orderItem.badge.delete()
    orderItem.delete()

def handler(obj):
    if hasattr(obj, 'isoformat'):
        return obj.isoformat()
    elif isinstance(obj, Decimal):
        return str(obj)
    else:
        raise TypeError('Object of type %s with value of %s is not JSON serializable' % (type(obj), repr(obj)))

def getRegistrationEmail(event=None):
    """
    Retrieves the email address to show on error messages in the attendee
    registration form for a specified event.  If no event specified, uses
    the first default event.  If no email is listed there, returns the
    default of APIS_DEFAULT_EMAIL in settings.py.
    """
    if event is None: 
        try:
            event = Event.objects.get(default=True)
        except:
            return settings.APIS_DEFAULT_EMAIL
    if event.registrationEmail == '':
        return settings.APIS_DEFAULT_EMAIL
    return event.registrationEmail

def getStaffEmail(event=None):
    """
    Retrieves the email address to show on error messages in the staff
    registration form for a specified event.  If no event specified, uses
    the first default event.  If no email is listed there, returns the
    default of APIS_DEFAULT_EMAIL in settings.py.
    """
    if event is None: 
        try:
            event = Event.objects.get(default=True)
        except:
            return settings.APIS_DEFAULT_EMAIL
    if event.staffEmail == '':
        return settings.APIS_DEFAULT_EMAIL
    return event.staffEmail

def getDealerEmail(event=None):
    """
    Retrieves the email address to show on error messages in the dealer
    registration form for a specified event.  If no event specified, uses
    the first default event.  If no email is listed there, returns the
    default of APIS_DEFAULT_EMAIL in settings.py.
    """
    if event is None: 
        try:
            event = Event.objects.get(default=True)
        except:
            return settings.APIS_DEFAULT_EMAIL
    if event.dealerEmail == '':
        return settings.APIS_DEFAULT_EMAIL
    return event.dealerEmail

# vim: ts=4 sts=4 sw=4 expandtab smartindent
