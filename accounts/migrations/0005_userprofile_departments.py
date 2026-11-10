# 一个账号可属于多个部门：UserProfile.departments 多对多 + 「通用」成为默认分配的部门。
from django.db import migrations, models

GENERAL = "通用"


def seed_departments(apps, schema_editor):
    """登记「通用」，并把每个账号已有的主部门回填进多选集合。"""
    Department = apps.get_model("accounts", "Department")
    UserProfile = apps.get_model("accounts", "UserProfile")
    general, _ = Department.objects.get_or_create(name=GENERAL)
    for profile in UserProfile.objects.select_related("user").prefetch_related("departments"):
        names = {GENERAL}
        if (profile.department or "").strip():
            names.add(profile.department.strip())
        names.update(d.name for d in profile.departments.all())
        rows = [general]
        for name in sorted(names - {GENERAL}):
            rows.append(Department.objects.get_or_create(name=name)[0])
        profile.departments.set(rows)


def drop_seeded_departments(apps, schema_editor):
    """回退：清空多选（「通用」登记行保留，它不影响旧行为）。"""
    UserProfile = apps.get_model("accounts", "UserProfile")
    for profile in UserProfile.objects.all():
        profile.departments.clear()


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0004_individual_kb_role_help"),
    ]

    operations = [
        migrations.AddField(
            model_name="userprofile",
            name="departments",
            field=models.ManyToManyField(
                blank=True, help_text="一个账号可属于多个部门，可见范围＝「通用」∪ 这里勾选的全部部门。"
                                      "「通用」默认勾选且不可取消。",
                related_name="user_profiles", to="accounts.department",
                verbose_name="部门（可多选）",
            ),
        ),
        migrations.AlterField(
            model_name="userprofile",
            name="department",
            field=models.CharField(
                default=GENERAL, help_text="默认归属部门：新建知识库、个人库的部门，以及「通用」资料的维护权都按它判定；"
                                           "可见范围按「部门」多选（可多个）。",
                max_length=50, verbose_name="主部门",
            ),
        ),
        migrations.AlterField(
            model_name="userprofile",
            name="role",
            field=models.CharField(
                choices=[("user", "普通用户"), ("dept_admin", "部门管理员")],
                default="user",
                help_text="部门管理员可管理其所辖各部门的共享知识库；所有用户可创建和管理仅本人可访问的个人知识库。",
                max_length=20, verbose_name="账号级别",
            ),
        ),
        migrations.RunPython(seed_departments, drop_seeded_departments),
    ]
