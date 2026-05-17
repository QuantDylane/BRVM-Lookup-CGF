from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0008_fondamentaux_annuels"),
    ]

    operations = [
        migrations.DeleteModel(name="AgentAlerte"),
        migrations.DeleteModel(name="AgentLog"),
        migrations.DeleteModel(name="AgentDependency"),
        migrations.DeleteModel(name="AgentTask"),
        migrations.DeleteModel(name="AgentConfig"),
    ]
