import os
import PIL
import pyminizip
import pypdfium2 as pdfium
from PIL import Image

from datetime import datetime
from django.apps import apps as django_apps
from django.conf import settings
from django.contrib.auth.models import User, Group
from django.core.exceptions import ValidationError
from django.db.models import Q
from edc_action_item.site_action_items import site_action_items
from edc_base.utils import get_utcnow
from edc_constants.constants import OPEN, NEW
from edc_data_manager.models import DataActionItem


class ChildUtils:

    subject_schedule_history_model = 'edc_visit_schedule.subjectschedulehistory'
    registered_subject_model = 'edc_registration.registeredsubject'
    child_dummy_consent_model = 'flourish_child.childdummysubjectconsent'
    preg_screening_model = 'flourish_caregiver.screeningpregwomen'
    prior_screening_model = 'flourish_caregiver.screeningpriorbhpparticipants'
    consent_version_model = 'flourish_caregiver.flourishconsentversion'
    child_assent_model = 'flourish_child.childassent'

    @property
    def child_assent_model_cls(self):
        return django_apps.get_model(self.child_assent_model)

    @property
    def child_dummy_consent_model_cls(self):
        return django_apps.get_model(self.child_dummy_consent_model)

    @property
    def preg_screening_model_cls(self):
        return django_apps.get_model(self.preg_screening_model)

    @property
    def prior_screening_model_cls(self):
        return django_apps.get_model(self.prior_screening_model)

    @property
    def registered_subject_cls(self):
        return django_apps.get_model(self.registered_subject_model)

    @property
    def subject_schedule_history_cls(self):
        return django_apps.get_model(self.subject_schedule_history_model)

    @property
    def consent_version_cls(self):
        return django_apps.get_model(self.consent_version_model)

    def caregiver_subject_identifier(self, subject_identifier=None):
        childconsent_obj = self.child_dummy_consent_model_cls.objects.filter(
            subject_identifier=subject_identifier).last()

        return getattr(childconsent_obj, 'relative_identifier', None)

    def child_assent_obj(self, subject_identifier):
        try:
            child_assent = self.child_assent_model_cls.objects.get(
                subject_identifier=subject_identifier)
        except self.child_assent_model_cls.DoesNotExist:
            pass
        else:
            return child_assent

    def preg_screening_model_obj(self, subject_identifier=None):
        caregiver_sid = self.caregiver_subject_identifier(
            subject_identifier=subject_identifier)
        try:
            preg_screening = self.preg_screening_model_cls.objects.get(
                subject_identifier=caregiver_sid)
        except self.preg_screening_model_cls.DoesNotExist:
            return None
        else:
            return preg_screening

    def prior_screening_model_obj(self, subject_identifier=None):
        caregiver_sid = self.caregiver_subject_identifier(
            subject_identifier=subject_identifier)
        try:
            prior_screening = self.prior_screening_model_cls.objects.get(
                subject_identifier=caregiver_sid)
        except self.prior_screening_model_cls.DoesNotExist:
            return None
        else:
            return prior_screening

    def consent_version(self, subject_identifier):
        subject_screening_obj = self.preg_screening_model_obj(
            subject_identifier) or self.prior_screening_model_obj(
                subject_identifier)

        if not subject_screening_obj:
            raise ValidationError(
                'Missing Subject Screening form. Please complete '
                'it before proceeding.')

        try:
            consent_version_obj = self.consent_version_cls.objects.get(
                screening_identifier=subject_screening_obj.screening_identifier)
        except self.consent_version_cls.DoesNotExist:
            raise ValidationError(
                'Missing Consent Version form. Please complete '
                'it before proceeding.')
        return consent_version_obj.child_version or consent_version_obj.version


    def get_onschedule_names(self, instance):
        onschedules = self.subject_schedule_history_cls.objects.filter(
            subject_identifier=instance.subject_identifier).exclude(
                Q(schedule_name__icontains='tb') | Q(schedule_name__icontains='facet')).values_list(
                    'schedule_name', flat=True)
        return list(onschedules)


    def get_previous_appt_instance(self, appointment):
        schedule_names = self.get_onschedule_names(appointment)
        try:
            previous_appt = appointment.__class__.objects.filter(
                subject_identifier=appointment.subject_identifier,
                appt_datetime__lt=appointment.appt_datetime,
                schedule_name__in=schedule_names,
                visit_code_sequence=0).latest('appt_datetime')
        except appointment.__class__.DoesNotExist:
            return appointment.previous_by_timepoint
        else:
            return previous_appt

child_utils = ChildUtils()


def notification(subject_identifier, subject, user_created,
                 group_names=('assignable users',), comment=''):
    if user_created:
        try:
            user = User.objects.get(username=user_created)
        except User.DoesNotExist:
            pass
        else:
            try:
                user.groups.get(name__in=group_names)
            except Group.DoesNotExist:
                groups = Group.objects.filter(name__in=group_names)
                for group in groups:
                    user.groups.add(group)
                user.save()
            finally:
                DataActionItem.objects.create(
                    subject_identifier=subject_identifier,
                    user_created=user_created,
                    status=OPEN,
                    action_priority='high',
                    assigned=user.username,
                    subject=subject,
                    comment=comment)


def trigger_action_item(model_cls, action_name, subject_identifier, repeat=False):
    action_cls = site_action_items.get(
        model_cls.action_name)
    action_item_model_cls = action_cls.action_item_model_cls()

    try:
        model_cls.objects.get(subject_identifier=subject_identifier)
    except model_cls.DoesNotExist:
        trigger = True
    else:
        trigger = repeat
    if trigger:
        try:
            action_item_obj = action_item_model_cls.objects.get(
                subject_identifier=subject_identifier,
                action_type__name=action_name)
        except action_item_model_cls.DoesNotExist:
            action_cls = site_action_items.get(action_name)
            action_cls(subject_identifier=subject_identifier)
        else:
            action_item_obj.status = OPEN
            action_item_obj.save()
    else:
        try:
            action_item = action_item_model_cls.objects.get(
                Q(status=NEW) | Q(status=OPEN),
                subject_identifier=subject_identifier,
                action_type__name=action_name)
        except action_item_model_cls.DoesNotExist:
            pass
        else:
            action_item.delete()


def stamp_image(instance):
    filefield = instance.image
    filename = filefield.name  # gets the "normal" file name as it was uploaded
    storage = filefield.storage
    path = storage.path(filename)
    if '.pdf' not in path:
        base_image = Image.open(path)
        stamped_img = add_image_stamp(base_image=base_image)
        stamped_img.save(path)
    else:
        print_pdf(path)


def add_image_stamp(base_image=None, position=(25, 25), resize=(500, 500)):
    """
    Superimpose image of a stamp over copy of the base image
    @param image_path: dir to base image
    @param dont_save: boolean for not saving the image just converting
    @param position: pixels(w,h) to superimpose stamp at
    """
    stamp = Image.open('media/stamp/true-copy.png')
    if resize:
        stamp = stamp.resize(resize, PIL.Image.ANTIALIAS)

    width, height = base_image.size
    stamp_width, stamp_height = stamp.size

    # Determine orientation of the base image before pasting stamp
    if width < height:
        pos_width = round(width / 2) - round(stamp_width / 2)
        pos_height = height - stamp_height
        position = (pos_width, pos_height)
    elif width > height:
        stamp = stamp.rotate(90)
        pos_width = width - stamp_width
        pos_height = round(height / 2) - round(stamp_height / 2)
        position = (pos_width, pos_height)

    # paste stamp over image
    base_image.paste(stamp, position, mask=stamp)
    return base_image


def encrypt_files(instance, subject_identifier):
    base_path = settings.MEDIA_ROOT
    if instance.image:
        upload_to = f'{instance.image.field.upload_to}'
        timestamp = datetime.timestamp(get_utcnow())
        zip_filename = f'{subject_identifier}_{timestamp}.zip'
        with open('filekey.key', 'r') as filekey:
            key = filekey.read().rstrip()
        com_lvl = 8
        pyminizip.compress(f'{instance.image.path}', None,
                           f'{base_path}/{upload_to}{zip_filename}', key, com_lvl)
    # remove unencrypted file
    if os.path.exists(f'{instance.image.path}'):
        os.remove(f'{instance.image.path}')
    instance.image = f'{upload_to}{zip_filename}'
    instance.save()


def print_pdf(filepath):
    pdf = pdfium.PdfDocument(filepath)
    page_indices = [i for i in range(len(pdf))]
    renderer = pdf.render_to(
        pdfium.BitmapConv.pil_image,
        page_indices=page_indices,
        scale=300 / 72
    )
    stamped_pdf_images = []
    for image, _ in zip(renderer, page_indices):
        stamped_pdf_images.append(add_image_stamp(base_image=image))
    first_img = stamped_pdf_images[0]
    first_img.save(filepath, save_all=True,
                   append_images=stamped_pdf_images[1:])
