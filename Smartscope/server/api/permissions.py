from django.contrib.auth.models import Group
from django.db.models import Q
from rest_framework import permissions
import logging

logger = logging.getLogger(__name__)


def is_in_group(user, group_name):
    """
    Takes a user and a group name, and returns `True` if the user is in that group.
    """
    try:
        return Group.objects.get(name=group_name).user_set.filter(id=user.id).exists()
    except Group.DoesNotExist:
        return None


def user_group_names(user):
    """Names of the groups the user belongs to. ScreeningSession.group points at Group.name."""
    return list(user.groups.values_list('name', flat=True))


def scope_queryset_to_user_groups(queryset, user, group_lookup):
    """
    Restrict a queryset to objects whose session group is one of the user's groups.

    `group_lookup` is the ORM path from the queryset's model to ScreeningSession.group,
    e.g. 'group' for sessions or 'grid_id__session_id__group' for targets.
    Staff see everything, matching HasGroupPermission. Objects whose session has no
    group are hidden from non-staff, matching HasGroupPermission's detail check.
    """
    if user is None or not user.is_authenticated:
        return queryset.none()
    if user.is_staff:
        return queryset
    group_names = user_group_names(user)
    logger.debug(f'Scoping {queryset.model.__name__} to groups {group_names} for {user}')
    return queryset.filter(**{f'{group_lookup}__in': group_names})


def scope_target_labels_to_user_groups(queryset, user):
    """
    Restrict Finder/Classifier/Selector rows (generic relations to a target) to labels
    whose target belongs to a session in one of the user's groups.
    """
    from django.contrib.contenttypes.models import ContentType
    from Smartscope.core.models import AtlasModel, SquareModel, HoleModel, HighMagModel

    if user is None or not user.is_authenticated:
        return queryset.none()
    if user.is_staff:
        return queryset
    group_names = user_group_names(user)
    allowed = Q()
    for target_model in (AtlasModel, SquareModel, HoleModel, HighMagModel):
        target_pks = target_model.objects.filter(
            grid_id__session_id__group__in=group_names).values('pk')
        content_type = ContentType.objects.get_for_model(target_model)
        allowed |= Q(content_type=content_type, object_id__in=target_pks)
    logger.debug(f'Scoping {queryset.model.__name__} to groups {group_names} for {user}')
    return queryset.filter(allowed)


class GroupScopedQuerysetMixin:
    """
    Viewset mixin: list, detail and every action that goes through get_queryset() only
    see objects in the requesting user's groups (staff see all).

    Must come before the DRF viewset class in the bases so its get_queryset() wins.
    Set `group_lookup` to the ORM path to ScreeningSession.group.
    """
    group_lookup = None

    def get_queryset(self):
        if self.group_lookup is None:
            raise ValueError(f'group_lookup is not set on {self.__class__.__name__}.')
        queryset = super().get_queryset()
        return scope_queryset_to_user_groups(queryset, self.request.user, self.group_lookup)


class HasGroupPermission(permissions.BasePermission):
    """
    Ensure user is in required groups.
    """

    def has_object_permission(self, request, view, obj):
        if request.user.is_staff:
            logger.debug(f'{request.user} is staff, permission granted')
            return True
        group_name = obj.group
        logger.debug(f'Checking if user is in group {group_name}')

        return is_in_group(request.user, group_name)
