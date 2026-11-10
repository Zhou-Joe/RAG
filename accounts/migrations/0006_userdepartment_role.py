# 每个部门的级别可以不同：M2M 关联表升级为带 role 的 through 模型（UserDepartment）。
from django.db import migrations, models

GENERAL = "通用"


def copy_memberships(apps, schema_editor):
    """把 0005 的纯关联行搬进新的「部门身份」表，并按旧规则补 role。

    旧模型只有一个账号级 role，展开成逐部门级别时：
    - 具体部门 → 沿用旧 role（原来能管本部门库的照旧能管）；
    - 「通用」 → 只有主部门就是「通用」的部门管理员算管理员（旧行为里公共库的维护者），
      其余账号在通用部门里只是成员，否则「人人默认挂通用」等于把公共库编辑权发给全站。
    """
    UserProfile = apps.get_model("accounts", "UserProfile")
    Department = apps.get_model("accounts", "Department")
    UserDepartment = apps.get_model("accounts", "UserDepartment")
    names = {d.pk: d.name for d in Department.objects.all()}
    legacy_pairs = set()
    if "accounts_userprofile_departments" in schema_editor.connection.introspection.table_names():
        with schema_editor.connection.cursor() as cur:
            cur.execute("select userprofile_id, department_id "
                        "from accounts_userprofile_departments")
            legacy_pairs = {(p, d) for p, d in cur.fetchall()}
    for profile in UserProfile.objects.all():
        linked = {d for (p, d) in legacy_pairs if p == profile.pk}
        for name, pk in names.items():
            if name == GENERAL or name == (profile.department or ""):
                linked.add(pk)          # 「通用」人人都有；主部门也算在内
        for dept_id in linked:
            name = names.get(dept_id)
            if not name:
                continue
            if name == GENERAL:
                # 只有旧数据里“主部门就是通用”的部门管理员能维护公共库
                role = ("dept_admin" if profile.department == GENERAL
                        and profile.role == "dept_admin" else "user")
            else:
                role = profile.role or "user"
            UserDepartment.objects.update_or_create(
                profile_id=profile.pk, department_id=dept_id, defaults={"role": role},
            )


def drop_memberships(apps, schema_editor):
    """回退：删掉部门身份行（主部门字段还在，旧逻辑照常工作）。"""
    apps.get_model("accounts", "UserDepartment").objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0005_userprofile_departments"),
    ]

    operations = [
        migrations.CreateModel(
            name="UserDepartment",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True,
                                           serialize=False, verbose_name="ID")),
                ("role", models.CharField(
                    choices=[("user", "普通用户"), ("dept_admin", "部门管理员")],
                    default="user", max_length=20, verbose_name="该部门级别")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("department", models.ForeignKey(
                    on_delete=models.deletion.CASCADE, related_name="memberships",
                    to="accounts.department", verbose_name="部门")),
                ("profile", models.ForeignKey(
                    on_delete=models.deletion.CASCADE, related_name="memberships",
                    to="accounts.userprofile", verbose_name="用户")),
            ],
            options={
                "verbose_name": "部门身份",
                "verbose_name_plural": "部门身份",
                "ordering": ["department__name"],
            },
        ),
        migrations.AddConstraint(
            model_name="userdepartment",
            constraint=models.UniqueConstraint(fields=("profile", "department"),
                                               name="unique_user_department"),
        ),
        migrations.RunPython(copy_memberships, drop_memberships),
        # M2M 不能直接 AlterField 改 through（Django 不允许），只能在**状态**里
        # 拆旧建新：数据库这边新表已由 UserDepartment 建好，旧关联表下面单独 DROP。
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.RemoveField(model_name="userprofile", name="departments"),
                migrations.AddField(
                    model_name="userprofile",
                    name="departments",
                    field=models.ManyToManyField(
                        blank=True, help_text="一个账号可属于多个部门，可见范围＝「通用」∪ 全部部门。"
                                              "「通用」默认在内且不可取消；每个部门里的级别单独设置。",
                        related_name="user_profiles", through="accounts.UserDepartment",
                        to="accounts.department", verbose_name="部门（可多选）",
                    ),
                ),
            ],
            database_operations=[],
        ),
        # 0005 的自动关联表已被上面的 through 表取代，留着只会让人误以为还能直接写它
        migrations.RunSQL(
            sql="DROP TABLE IF EXISTS accounts_userprofile_departments",
            reverse_sql=(
                "CREATE TABLE accounts_userprofile_departments ("
                "id integer NOT NULL PRIMARY KEY, userprofile_id bigint NOT NULL "
                "REFERENCES accounts_userprofile(id) DEFERRABLE INITIALLY DEFERRED, "
                "department_id bigint NOT NULL REFERENCES accounts_department(id) "
                "DEFERRABLE INITIALLY DEFERRED); "
                "INSERT INTO accounts_userprofile_departments "
                "(id, userprofile_id, department_id) SELECT id, profile_id, department_id "
                "FROM accounts_userdepartment;"
            ),
        ),
        migrations.AlterField(
            model_name="userprofile",
            name="department",
            field=models.CharField(
                default=GENERAL, help_text="默认归属部门：新建知识库与个人库落在它身上；"
                                           "可见范围看部门集合，管理权看逐部门级别。",
                max_length=50, verbose_name="主部门",
            ),
        ),
        migrations.AlterField(
            model_name="userprofile",
            name="role",
            field=models.CharField(
                choices=[("user", "普通用户"), ("dept_admin", "部门管理员")],
                default="user",
                help_text="汇总值：任一部门是部门管理员即部门管理员。新挂部门默认普通用户。"
                          "逐部门的级别在「部门身份」里设置；所有用户都可管理仅本人可见的个人知识库。",
                max_length=20, verbose_name="账号级别",
            ),
        ),
    ]
